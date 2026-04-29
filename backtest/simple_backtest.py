"""回测引擎 v4 — 修复4个P0 Bug + 真正组合级模拟

Bug①  _apply_pnl 用入场 capital
Bug②  1.5x 平仓用 remaining × 50%
Bug③  归一化评分 momentum×0.54 + oi×0.31 + whitelist×0.15
Bug④  组合级模拟 — 所有币共享资金池
"""

from datetime import timedelta
from dataclasses import dataclass

import pandas as pd
import structlog

from config.settings import settings

log = structlog.get_logger()


def _compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def _compute_scores(df: pd.DataFrame) -> pd.DataFrame:
    """Bug③: 归一化评分。"""
    df = df.copy()
    df["return"] = df["close"].pct_change()
    df["vol_20d"] = df["return"].rolling(20 * 288).std()
    df["change_pct"] = df["return"] * 100
    df["atr"] = _compute_atr(df, period=14)

    mt = settings.momentum_threshold
    mask = df["vol_20d"] > 0
    momentum = pd.Series(0.0, index=df.index)
    if mask.any():
        triggered = df["change_pct"].abs() / 100 > mt * df["vol_20d"]
        if triggered.any():
            intensity = df.loc[triggered, "change_pct"].abs() / 100 / (mt * df.loc[triggered, "vol_20d"])
            momentum.loc[triggered] = 0.3 + 0.7 * (intensity.clip(upper=3.0) / 3.0)

    oi = pd.Series(0.0, index=df.index)
    if "open_interest" in df.columns and df["open_interest"].notna().any():
        oi_filled = df["open_interest"].ffill()
        div = df["close"].pct_change(12) * (-oi_filled.pct_change(12))
        oi = div.clip(lower=0, upper=1).fillna(0)

    wl = pd.Series(0.0, index=df.index)
    dv = df["return"].rolling(288).std() * 288**0.5
    if (dv > 0.3).any():
        wl[dv > 0.3] = 0.5

    df["score"] = 0.54 * momentum + 0.31 * oi + 0.15 * wl
    return df


@dataclass
class Position:
    symbol: str
    entry_price: float
    entry_time: pd.Timestamp
    entry_capital: float
    stop_loss: float
    stop_loss_distance: float
    remaining_pct: float
    accum_pnl: float = 0.0
    half_closed: bool = False
    closed_3x: bool = False
    peak_pnl_ratio: float = 0.0


def run_backtest(
    thresholds: list[float] = None,
    initial_capital: float = 100.0,
    risk_per_trade: float = 0.02,
    taker_fee_rate: float = 0.0004,
    slippage_rate: float = 0.001,
    max_positions: int = 5,
) -> pd.DataFrame:
    """Bug④: 组合级模拟。只在有信号或有持仓的时间点遍历。"""
    from backtest.data_downloader import download_top_symbols

    if thresholds is None:
        thresholds = [round(0.30 + i * 0.05, 2) for i in range(8)]

    log.info("backtest_start", thresholds=thresholds)
    raw_data = download_top_symbols(top_n=30, days=90)
    data = {sym: _compute_scores(df) for sym, df in raw_data.items()}
    log.info("data_loaded", symbols=len(data))

    # 预处理：每个币转为 {timestamp: row_dict}，用 dict 加速查找
    sym_data = {}
    for sym, df in data.items():
        rows = {}
        for i, row in df.iterrows():
            ts = row["timestamp"]
            rows[ts] = {"close": row["close"], "atr": row.get("atr", 0), "score": row.get("score", 0)}
        sym_data[sym] = rows

    lev = settings.leverage_strategy_a
    results = []

    for threshold in thresholds:
        # 收集事件时间点：score >= threshold 的行 + 持仓需要监控的时间点
        # 用集合去重
        event_times = set()
        signal_at = {}  # (ts, sym) -> row_data
        for sym, rows in sym_data.items():
            for ts, rd in rows.items():
                if rd["score"] >= threshold:
                    event_times.add(ts)
                    signal_at[(ts, sym)] = rd
                # 也加入相邻时间点用于持仓监控（简化：加入所有时间戳太慢）
                # 实际上我们用所有时间戳但只遍历有持仓的币

        # 实际上需要所有时间戳来监控持仓退出
        # 但只遍历有持仓的币 → 用时间戳索引
        all_ts = sorted(set().union(*(set(r.keys()) for r in sym_data.values())))

        capital = initial_capital
        peak_capital = capital
        max_drawdown = 0.0
        positions: dict[str, Position] = {}
        round_trips = []

        def _apply_pnl(pnl_pct, size_pct, entry_cap):
            pv = entry_cap * size_pct
            return pv * pnl_pct * lev - pv * slippage_rate * lev - pv * taker_fee_rate * lev * 2

        def _close(pos, ts, price, reason):
            nonlocal capital
            pnl_pct = (price - pos.entry_price) / pos.entry_price
            net = _apply_pnl(pnl_pct, pos.remaining_pct, pos.entry_capital)
            capital += net
            capital = max(capital, 0)  # 不欠钱
            pos.accum_pnl += net
            round_trips.append({
                "entry_time": pos.entry_time, "exit_time": ts,
                "entry_price": pos.entry_price, "exit_price": price,
                "pnl": pos.accum_pnl, "exit_reason": reason,
                "hold_hours": (ts - pos.entry_time).total_seconds() / 3600,
                "symbol": pos.symbol,
            })

        for ts in all_ts:
            # 只遍历有持仓的 + 有信号的币
            syms_to_check = set(positions.keys())
            # 加入有信号的币
            for sym in sym_data:
                if (ts, sym) in signal_at:
                    syms_to_check.add(sym)

            for sym in syms_to_check:
                rows = sym_data[sym]
                if ts not in rows:
                    continue
                rd = rows[ts]
                price = rd["close"]
                atr = rd["atr"]

                # ── 持仓处理 ──
                if sym in positions:
                    pos = positions[sym]
                    stop_dist = pos.stop_loss_distance
                    closed = False

                    if price <= pos.stop_loss:
                        _close(pos, ts, pos.stop_loss, "stop_loss")
                        del positions[sym]
                        closed = True

                    if not closed:
                        pnl_ratio = (price - pos.entry_price) / stop_dist if stop_dist > 0 else 0
                        if pnl_ratio > pos.peak_pnl_ratio:
                            pos.peak_pnl_ratio = pnl_ratio

                        if (ts - pos.entry_time) > timedelta(hours=48):
                            _close(pos, ts, price, "timeout_48h")
                            del positions[sym]
                            closed = True
                        elif pnl_ratio >= 5:
                            _close(pos, ts, price, "take_profit_5x")
                            del positions[sym]
                            closed = True

                    if not closed and sym in positions:
                        pos = positions[sym]
                        stop_dist = pos.stop_loss_distance
                        pnl_ratio = (price - pos.entry_price) / stop_dist if stop_dist > 0 else 0

                        if pnl_ratio >= 3 and not pos.closed_3x:
                            cp = pos.remaining_pct * 0.75
                            net = _apply_pnl((price - pos.entry_price) / pos.entry_price, cp, pos.entry_capital)
                            capital += net
                            pos.accum_pnl += net
                            pos.remaining_pct -= cp
                            pos.closed_3x = True
                            pos.stop_loss = pos.entry_price * 1.001

                        pnl_ratio2 = (price - pos.entry_price) / stop_dist if stop_dist > 0 else 0
                        if pnl_ratio2 >= 1.5 and not pos.half_closed:
                            cp = pos.remaining_pct * 0.5
                            net = _apply_pnl((price - pos.entry_price) / pos.entry_price, cp, pos.entry_capital)
                            capital += net
                            pos.accum_pnl += net
                            pos.half_closed = True
                            pos.remaining_pct -= cp
                            pos.stop_loss = pos.entry_price

                        if pos.half_closed and pos.peak_pnl_ratio > 1.5:
                            dd = pos.peak_pnl_ratio - pnl_ratio2
                            if dd >= pos.peak_pnl_ratio * 0.4:
                                _close(pos, ts, price, "trailing_stop")
                                del positions[sym]

                # ── 开仓（限制同时持仓数 + 可用资金）──
                if sym not in positions and len(positions) < max_positions:
                    score = rd["score"]
                    if score >= threshold and not pd.isna(score) and atr > 0:
                        stop_dist = 2 * atr
                        if stop_dist > 0 and capital > 0:
                            # 可用资金 = 总资金 - 已占用资金（每个持仓的剩余百分比 × 入场资金）
                            committed = sum(p.entry_capital * p.remaining_pct for p in positions.values())
                            available = max(capital - committed, 0)
                            if available < 1:  # 最低 1u 才开仓
                                continue
                            size_pct = min(available * risk_per_trade / (stop_dist * lev), 1.0)
                            if size_pct > 0:
                                positions[sym] = Position(
                                    symbol=sym, entry_price=price, entry_time=ts,
                                    entry_capital=available,  # 用可用资金，不是全部资金
                                    stop_loss=price - stop_dist,
                                    stop_loss_distance=stop_dist,
                                    remaining_pct=size_pct,
                                )

            if capital > peak_capital:
                peak_capital = capital
            if peak_capital > 0:
                max_drawdown = max(max_drawdown, (peak_capital - capital) / peak_capital)

        for sym, pos in list(positions.items()):
            _close(pos, all_ts[-1], pos.entry_price, "force_close")

        # 统计
        total = len(round_trips)
        if total == 0:
            results.append({"threshold": threshold, "trades": 0, "win_rate": 0,
                            "profit_ratio": 0, "total_pnl": 0, "max_dd%": 0,
                            "avg_hold_h": 0, "timeout%": 0,
                            "stop_loss": 0, "trailing": 0, "tp_5x": 0, "timeout": 0})
            continue

        wins = [t for t in round_trips if t["pnl"] > 0]
        losses = [t for t in round_trips if t["pnl"] <= 0]
        wr = len(wins) / total * 100
        aw = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        al = sum(abs(t["pnl"]) for t in losses) / len(losses) if losses else 0
        pr = aw / al if al > 0 else float("inf")
        tc = sum(1 for t in round_trips if t["exit_reason"] == "timeout_48h")

        results.append({
            "threshold": threshold, "trades": total,
            "win_rate": round(wr, 1), "profit_ratio": round(pr, 2),
            "total_pnl": round(capital - initial_capital, 2),
            "max_dd%": round(max_drawdown * 100, 1),
            "avg_hold_h": round(sum(t["hold_hours"] for t in round_trips) / total, 1),
            "timeout%": round(tc / total * 100, 1),
            "stop_loss": sum(1 for t in round_trips if t["exit_reason"] == "stop_loss"),
            "trailing": sum(1 for t in round_trips if t["exit_reason"] == "trailing_stop"),
            "tp_5x": sum(1 for t in round_trips if t["exit_reason"] == "take_profit_5x"),
            "timeout": tc,
        })

    return pd.DataFrame(results)


if __name__ == "__main__":
    report = run_backtest()
    print("\n" + "=" * 120)
    print("回测 v4 — 真正组合级模拟 | Bug①②③④ | 归一化评分 | ATR仓位 | 手续费+滑点")
    print("=" * 120)
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
