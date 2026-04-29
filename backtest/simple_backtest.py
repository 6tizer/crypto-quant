"""回测引擎 v3 — ATR 自适应仓位 + Trailing Stop + 权重归一化评分"""

from datetime import timedelta

import pandas as pd
import structlog

from config.settings import settings

log = structlog.get_logger()


def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """计算 ATR (Average True Range)。"""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)

    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def simulate_strategy_a(
    df: pd.DataFrame,
    entry_threshold: float = 0.65,
    initial_capital: float = 100.0,
    taker_fee_rate: float = 0.0004,
    risk_per_trade: float = 0.02,
    slippage_rate: float = 0.001,
) -> dict:
    """策略 A 回测（v3）。

    改进：
    - 止损 = 2 × ATR(14)，每个币独立
    - 仓位 = 账户净值 × risk_per_trade / (止损距离 × 杠杆)
    - 止盈：1.5x 平半 + trailing → 3x 平剩余 75% → 5x 清仓 → 48h 兜底
    - Trailing stop：从最高盈利回撤 40% 就平
    - 手续费 + 滑点
    - 所有部分平仓合并为单笔 round-trip 统计
    """
    if len(df) < 200:
        return {"trades": 0}

    df = df.copy()
    df["return"] = df["close"].pct_change()
    df["vol_20d"] = df["return"].rolling(20 * 288).std()
    df["change_pct"] = df["return"] * 100
    df["atr"] = _compute_atr(df, period=14)

    # 入场信号
    momentum_threshold = settings.momentum_threshold
    df["signal"] = 0.0
    mask = df["vol_20d"] > 0
    df.loc[mask, "signal"] = (
        (df.loc[mask, "change_pct"].abs() / 100 > momentum_threshold * df.loc[mask, "vol_20d"]).astype(float)
    )

    # 连续评分：signal 触发时 score = 0.3 + 0.7 * (强度 / 3倍波动率)
    df["score"] = 0.0
    triggered = df["signal"] > 0
    if triggered.any():
        intensity = df.loc[triggered, "change_pct"].abs() / 100 / (momentum_threshold * df.loc[triggered, "vol_20d"])
        df.loc[triggered, "score"] = 0.3 + 0.7 * (intensity.clip(upper=3.0) / 3.0)

    lev = settings.leverage_strategy_a
    capital = initial_capital
    peak_capital = capital
    max_drawdown = 0.0

    # 每个 round-trip 记录
    round_trips = []
    # 当前持仓的部分平仓累计
    position = None

    def _calc_position_size(stop_distance):
        """ATR 自适应仓位：每笔最大亏损 = capital × risk_per_trade。"""
        if stop_distance <= 0 or capital <= 0:
            return 0
        size = capital * risk_per_trade / (stop_distance * lev)
        return min(size, 1.0)

    def _apply_pnl(pnl_pct, size_pct):
        """计算单次部分平仓的净盈亏（含手续费+滑点）。"""
        position_value = capital * size_pct
        gross_pnl = position_value * pnl_pct * lev
        slip = position_value * slippage_rate * lev
        fee = position_value * taker_fee_rate * lev * 2
        return gross_pnl - slip - fee

    for idx, row in df.iterrows():
        ts = row["timestamp"]
        price = row["close"]
        atr = row.get("atr", 0)

        if position:
            stop_distance = position["stop_loss_distance"]

            # 检查止损（用止损价而非收盘价，避免跳空超亏）
            if price <= position["stop_loss"]:
                remaining = position.get("remaining_pct", 0)
                exit_price = position["stop_loss"]  # 用止损价，不超亏
                pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"]
                net = _apply_pnl(pnl_pct, remaining)
                capital += net
                position["accum_pnl"] += net
                round_trips.append({
                    "entry_time": position["entry_time"],
                    "exit_time": ts,
                    "entry_price": position["entry_price"],
                    "exit_price": exit_price,
                    "pnl": position["accum_pnl"],
                    "exit_reason": "stop_loss",
                    "hold_hours": (ts - position["entry_time"]).total_seconds() / 3600,
                })
                position = None
                continue

            # 盈亏比
            pnl_ratio = (price - position["entry_price"]) / stop_distance if stop_distance > 0 else 0

            # 更新峰值盈利
            if pnl_ratio > position.get("peak_pnl_ratio", 0):
                position["peak_pnl_ratio"] = pnl_ratio

            # 48h 兜底
            if (ts - position["entry_time"]) > timedelta(hours=48):
                remaining = position.get("remaining_pct", 0)
                pnl_pct = (price - position["entry_price"]) / position["entry_price"]
                net = _apply_pnl(pnl_pct, remaining)
                capital += net
                position["accum_pnl"] += net
                round_trips.append({
                    "entry_time": position["entry_time"],
                    "exit_time": ts,
                    "entry_price": position["entry_price"],
                    "exit_price": price,
                    "pnl": position["accum_pnl"],
                    "exit_reason": "timeout_48h",
                    "hold_hours": (ts - position["entry_time"]).total_seconds() / 3600,
                })
                position = None
                continue

            # 5x 清仓
            if pnl_ratio >= 5:
                remaining = position.get("remaining_pct", 0)
                pnl_pct = (price - position["entry_price"]) / position["entry_price"]
                net = _apply_pnl(pnl_pct, remaining)
                capital += net
                position["accum_pnl"] += net
                round_trips.append({
                    "entry_time": position["entry_time"],
                    "exit_time": ts,
                    "entry_price": position["entry_price"],
                    "exit_price": price,
                    "pnl": position["accum_pnl"],
                    "exit_reason": "take_profit_5x",
                    "hold_hours": (ts - position["entry_time"]).total_seconds() / 3600,
                })
                position = None
                continue

            # 3x 平剩余 75%
            if pnl_ratio >= 3 and not position.get("closed_3x", False):
                remaining = position.get("remaining_pct", 0)
                close_pct = remaining * 0.75
                pnl_pct = (price - position["entry_price"]) / position["entry_price"]
                net = _apply_pnl(pnl_pct, close_pct)
                capital += net
                position["accum_pnl"] += net
                position["remaining_pct"] -= close_pct
                position["closed_3x"] = True
                position["stop_loss"] = position["entry_price"] * 1.001  # 保本

            # 1.5x 平半 + 启动 trailing
            if pnl_ratio >= 1.5 and not position.get("half_closed", False):
                pnl_pct = (price - position["entry_price"]) / position["entry_price"]
                net = _apply_pnl(pnl_pct, 0.5)
                capital += net
                position["accum_pnl"] += net
                position["half_closed"] = True
                position["remaining_pct"] = 0.5
                position["stop_loss"] = position["entry_price"]  # 保本止损

            # Trailing stop：从峰值回撤 40%
            if position.get("half_closed", False) and position.get("peak_pnl_ratio", 0) > 1.5:
                drawdown_from_peak = position["peak_pnl_ratio"] - pnl_ratio
                if drawdown_from_peak >= position["peak_pnl_ratio"] * 0.4:
                    remaining = position.get("remaining_pct", 0)
                    pnl_pct = (price - position["entry_price"]) / position["entry_price"]
                    net = _apply_pnl(pnl_pct, remaining)
                    capital += net
                    position["accum_pnl"] += net
                    round_trips.append({
                        "entry_time": position["entry_time"],
                        "exit_time": ts,
                        "entry_price": position["entry_price"],
                        "exit_price": price,
                        "pnl": position["accum_pnl"],
                        "exit_reason": "trailing_stop",
                        "hold_hours": (ts - position["entry_time"]).total_seconds() / 3600,
                    })
                    position = None
                    continue

        else:
            # 开仓
            score = row.get("score", 0)
            if score >= entry_threshold and not pd.isna(score) and atr > 0:
                stop_distance = 2 * atr
                stop_loss = price - stop_distance
                size_pct = _calc_position_size(stop_distance)
                if size_pct > 0 and capital > 0:
                    position = {
                        "entry_price": price,
                        "entry_time": ts,
                        "stop_loss": stop_loss,
                        "stop_loss_distance": stop_distance,
                        "remaining_pct": size_pct,
                        "accum_pnl": 0,
                        "half_closed": False,
                        "closed_3x": False,
                        "peak_pnl_ratio": 0,
                    }

        # 更新回撤
        if capital > peak_capital:
            peak_capital = capital
        if peak_capital > 0:
            dd = (peak_capital - capital) / peak_capital
            max_drawdown = max(max_drawdown, dd)

    # 统计（基于 round-trip）
    if not round_trips:
        return {"trades": 0}

    wins = [t for t in round_trips if t["pnl"] > 0]
    losses = [t for t in round_trips if t["pnl"] <= 0]

    return {
        "trades": len(round_trips),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(round_trips) * 100,
        "total_pnl": sum(t["pnl"] for t in round_trips),
        "final_capital": capital,
        "max_drawdown_pct": max_drawdown * 100,
        "avg_hold_hours": sum(t["hold_hours"] for t in round_trips) / len(round_trips),
        "trades_list": round_trips,
        "exit_reasons": {
            "stop_loss": sum(1 for t in round_trips if t["exit_reason"] == "stop_loss"),
            "trailing_stop": sum(1 for t in round_trips if t["exit_reason"] == "trailing_stop"),
            "timeout_48h": sum(1 for t in round_trips if t["exit_reason"] == "timeout_48h"),
            "take_profit_5x": sum(1 for t in round_trips if t["exit_reason"] == "take_profit_5x"),
        },
    }


def run_backtest(thresholds: list[float] = None) -> pd.DataFrame:
    """运行多阈值回测，每个阈值独立输出完整报告。"""
    from backtest.data_downloader import download_top_symbols

    if thresholds is None:
        thresholds = [round(0.30 + i * 0.05, 2) for i in range(8)]

    log.info("backtest_start", thresholds=thresholds)

    data = download_top_symbols(top_n=30, days=90)
    log.info("data_loaded", symbols=len(data))

    results = []
    for threshold in thresholds:
        agg = {
            "total_trades": 0, "total_wins": 0, "total_losses": 0,
            "total_pnl": 0,
            "sum_win_pnl": 0, "sum_loss_pnl": 0,
            "max_drawdown_pct": 0, "sum_hold_hours": 0,
            "symbols_tested": 0,
            "stop_loss": 0, "trailing": 0, "tp_5x": 0, "timeout": 0,
        }

        for sym, df in data.items():
            r = simulate_strategy_a(df, entry_threshold=threshold)
            if r["trades"] > 0:
                agg["total_trades"] += r["trades"]
                agg["total_wins"] += r["wins"]
                agg["total_losses"] += r["losses"]
                agg["total_pnl"] += r["total_pnl"]
                agg["sum_hold_hours"] += sum(t["hold_hours"] for t in r["trades_list"])
                agg["max_drawdown_pct"] = max(agg["max_drawdown_pct"], r["max_drawdown_pct"])
                agg["stop_loss"] += r["exit_reasons"]["stop_loss"]
                agg["trailing"] += r["exit_reasons"]["trailing_stop"]
                agg["tp_5x"] += r["exit_reasons"]["take_profit_5x"]
                agg["timeout"] += r["exit_reasons"]["timeout_48h"]
                agg["symbols_tested"] += 1
                agg["sum_win_pnl"] += sum(t["pnl"] for t in r["trades_list"] if t["pnl"] > 0)
                agg["sum_loss_pnl"] += sum(abs(t["pnl"]) for t in r["trades_list"] if t["pnl"] <= 0)

        total = agg["total_trades"]
        wr = agg["total_wins"] / total * 100 if total > 0 else 0
        avg_win = agg["sum_win_pnl"] / agg["total_wins"] if agg["total_wins"] > 0 else 0
        avg_loss = agg["sum_loss_pnl"] / agg["total_losses"] if agg["total_losses"] > 0 else 0
        profit_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")
        avg_hold = agg["sum_hold_hours"] / total if total > 0 else 0
        timeout_pct = agg["timeout"] / total * 100 if total > 0 else 0

        results.append({
            "threshold": threshold,
            "trades": total,
            "win_rate": round(wr, 1),
            "profit_ratio": round(profit_ratio, 2),
            "total_pnl": round(agg["total_pnl"], 2),
            "max_dd%": round(agg["max_drawdown_pct"], 1),
            "avg_hold_h": round(avg_hold, 1),
            "timeout%": round(timeout_pct, 1),
            "stop_loss": agg["stop_loss"],
            "trailing": agg["trailing"],
            "tp_5x": agg["tp_5x"],
            "timeout": agg["timeout"],
        })

    return pd.DataFrame(results)


if __name__ == "__main__":
    report = run_backtest()
    print("\n" + "=" * 110)
    print("回测 v3 — ATR自适应仓位(2%风险) + Trailing Stop + 手续费(0.04%/边) + 滑点(0.1%)")
    print("=" * 110)
    print(report.to_string(index=False))
    print()

    valid = report[(report["win_rate"] > 35) & (report["profit_ratio"] > 2) & (report["max_dd%"] < 30)]
    if len(valid) > 0:
        best = valid.loc[valid["total_pnl"].idxmax()]
        print(f"✅ 最优: 阈值={best['threshold']} | 胜率={best['win_rate']:.1f}% | 盈亏比={best['profit_ratio']:.2f} | "
              f"收益={best['total_pnl']:.2f}u | 回撤={best['max_dd%']:.1f}% | 交易={int(best['trades'])}笔")
    else:
        print("⚠️ 无阈值同时满足三条件（胜率>35%, 盈亏比>2, 回撤<30%）")
        best = report.loc[report["total_pnl"].idxmax()]
        print(f"按收益最优: 阈值={best['threshold']} | 胜率={best['win_rate']:.1f}% | 盈亏比={best['profit_ratio']:.2f} | "
              f"收益={best['total_pnl']:.2f}u | 回撤={best['max_dd%']:.1f}%")
