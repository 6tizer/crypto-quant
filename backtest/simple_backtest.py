"""简易回测引擎 — 用历史 K 线验证策略 A 信号和参数"""

from datetime import timedelta

import pandas as pd
import structlog

from config.settings import settings

log = structlog.get_logger()


def simulate_strategy_a(
    df: pd.DataFrame,
    entry_threshold: float = 0.65,
    stop_loss_pct: float = 0.20,
    initial_capital: float = 100.0,
) -> dict:
    """对单个币种模拟策略 A 交易。

    逻辑：
    - 每根 K 线计算涨跌幅，如果 > 2.5x 20 日波动率 → 触发入场信号
    - 信号分 > entry_threshold → 开仓做多
    - 止损 = entry × (1 - stop_loss_pct)
    - 止盈规则：3x保本 → 5x移止损 → 10x平半 → 48h强制平

    Returns:
        {trades, wins, losses, win_rate, avg_pnl_pct, max_drawdown, ...}
    """
    if len(df) < 100:
        return {"trades": 0}

    # 计算指标
    df = df.copy()
    df["return"] = df["close"].pct_change()
    df["vol_20d"] = df["return"].rolling(20 * 288).std()  # 20 days × 288 candles/day (5min)
    df["change_pct"] = df["return"] * 100

    # 入场信号：涨幅 > N × 20日波动率
    threshold = settings.momentum_threshold
    df["signal"] = 0.0
    mask = df["vol_20d"] > 0
    df.loc[mask, "signal"] = (df.loc[mask, "change_pct"].abs() / 100 > threshold * df.loc[mask, "vol_20d"]).astype(float)

    # 简化打分：signal 触发时给 0.5-1.0 的分数（基于强度）
    df.loc[df["signal"] > 0, "score"] = 0.5 + 0.5 * (df.loc[df["signal"] > 0, "change_pct"].abs() / df.loc[df["signal"] > 0, "change_pct"].abs().max())

    # 模拟交易
    capital = initial_capital
    peak_capital = capital
    max_drawdown = 0
    trades = []
    position = None  # {entry_price, entry_time, stop_loss, size}

    for _, row in df.iterrows():
        ts = row["timestamp"]
        price = row["close"]

        if position:
            # 检查止损
            if price <= position["stop_loss"]:
                pnl_pct = (price - position["entry_price"]) / position["entry_price"]
                pnl = capital * pnl_pct * settings.leverage_strategy_a
                capital += pnl
                trades.append({
                    "entry_time": position["entry_time"],
                    "exit_time": ts,
                    "entry_price": position["entry_price"],
                    "exit_price": price,
                    "pnl_pct": pnl_pct * 100,
                    "pnl": pnl,
                    "exit_reason": "stop_loss",
                    "hold_hours": (ts - position["entry_time"]).total_seconds() / 3600,
                })
                position = None
                continue

            # 检查止盈规则
            pnl_ratio = (price - position["entry_price"]) / (position["entry_price"] - position["stop_loss"]) if position["entry_price"] > position["stop_loss"] else 0

            # 48h 强制平
            if (ts - position["entry_time"]) > timedelta(hours=48):
                pnl_pct = (price - position["entry_price"]) / position["entry_price"]
                pnl = capital * pnl_pct * settings.leverage_strategy_a
                capital += pnl
                trades.append({
                    "entry_time": position["entry_time"],
                    "exit_time": ts,
                    "entry_price": position["entry_price"],
                    "exit_price": price,
                    "pnl_pct": pnl_pct * 100,
                    "pnl": pnl,
                    "exit_reason": "timeout_48h",
                    "hold_hours": (ts - position["entry_time"]).total_seconds() / 3600,
                })
                position = None
                continue

            # 10x 平半（简化：直接全平）
            if pnl_ratio >= 10:
                pnl_pct = (price - position["entry_price"]) / position["entry_price"]
                pnl = capital * pnl_pct * settings.leverage_strategy_a
                capital += pnl
                trades.append({
                    "entry_time": position["entry_time"],
                    "exit_time": ts,
                    "entry_price": position["entry_price"],
                    "exit_price": price,
                    "pnl_pct": pnl_pct * 100,
                    "pnl": pnl,
                    "exit_reason": "take_profit_10x",
                    "hold_hours": (ts - position["entry_time"]).total_seconds() / 3600,
                })
                position = None
                continue

            # 3x 保本
            if pnl_ratio >= 3 and position["stop_loss"] < position["entry_price"]:
                position["stop_loss"] = position["entry_price"] * 1.001

            # 5x 移止损
            if pnl_ratio >= 5:
                position["stop_loss"] = position["entry_price"] * 1.02

        else:
            # 开仓
            score = row.get("score", 0)
            if score >= entry_threshold and not pd.isna(score):
                stop_loss = price * (1 - stop_loss_pct)
                position = {
                    "entry_price": price,
                    "entry_time": ts,
                    "stop_loss": stop_loss,
                }

        # 更新回撤
        if capital > peak_capital:
            peak_capital = capital
        dd = (peak_capital - capital) / peak_capital if peak_capital > 0 else 0
        max_drawdown = max(max_drawdown, dd)

    # 统计
    if not trades:
        return {"trades": 0, "symbol": str(df["timestamp"].iloc[0]) if len(df) > 0 else "?"}

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
    profit_factor = avg_win / avg_loss if avg_loss > 0 else float("inf")

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "total_pnl": total_pnl,
        "final_capital": capital,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "avg_hold_hours": sum(t["hold_hours"] for t in trades) / len(trades),
        "max_drawdown_pct": max_drawdown * 100,
        "exit_reasons": {
            "stop_loss": sum(1 for t in trades if t["exit_reason"] == "stop_loss"),
            "take_profit_10x": sum(1 for t in trades if t["exit_reason"] == "take_profit_10x"),
            "timeout_48h": sum(1 for t in trades if t["exit_reason"] == "timeout_48h"),
        },
    }


def run_backtest(thresholds: list[float] = None) -> pd.DataFrame:
    """运行多阈值回测，输出对比报告。"""
    from backtest.data_downloader import download_top_symbols

    if thresholds is None:
        thresholds = [0.3, 0.4, 0.5, 0.65]

    log.info("backtest_start", thresholds=thresholds)

    # 下载数据（有缓存）
    data = download_top_symbols(top_n=30, days=90)  # 30 个够用
    log.info("data_loaded", symbols=len(data))

    results = []
    for threshold in thresholds:
        all_trades = []
        total_pnl = 0
        total_wins = 0
        total_losses = 0
        symbols_tested = 0

        for sym, df in data.items():
            r = simulate_strategy_a(df, entry_threshold=threshold)
            if r["trades"] > 0:
                all_trades.append(r)
                total_pnl += r["total_pnl"]
                total_wins += r["wins"]
                total_losses += r["losses"]
                symbols_tested += 1

        total = total_wins + total_losses
        win_rate = total_wins / total * 100 if total > 0 else 0
        avg_pnl = total_pnl / symbols_tested if symbols_tested > 0 else 0

        results.append({
            "threshold": threshold,
            "symbols_tested": symbols_tested,
            "total_trades": total,
            "wins": total_wins,
            "losses": total_losses,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_pnl_per_symbol": avg_pnl,
        })

    report = pd.DataFrame(results)
    return report


if __name__ == "__main__":
    report = run_backtest()
    print("\n" + "=" * 70)
    print("回测报告 — 策略 A（追高）不同入场阈值对比")
    print("=" * 70)
    print(report.to_string(index=False))
    print()

    # 找最优
    best = report.loc[report["win_rate"].idxmax()]
    print(f"最优阈值: {best['threshold']} | 胜率: {best['win_rate']:.1f}% | 总交易: {int(best['total_trades'])} | 总盈亏: {best['total_pnl']:.2f}u")
