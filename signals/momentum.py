"""涨幅异动 + OI 背离信号检测"""

import structlog
from sqlalchemy import desc

from config.settings import settings
from data.db.models import MarketSnapshot, get_session

log = structlog.get_logger()


def detect_momentum_anomaly(symbol: str, price_change_pct: float, volatility_20d: float) -> float:
    """涨幅异动检测。

    返回 0-1 分数：
    - price_change > N * volatility → 1.0（强触发）
    - price_change > N * 0.6 * volatility → 0.5（弱触发）
    - 其他 → 0.0

    Args:
        symbol: 币种符号
        price_change_pct: 涨跌幅（已经是百分比，如 +5.0 表示涨 5%）
        volatility_20d: 20 日波动率（小数，如 0.05 表示 5%）
    """
    if volatility_20d <= 0:
        return 0.0

    # 波动率是小数，price_change_pct 是百分比，统一到小数
    abs_change = abs(price_change_pct) / 100
    threshold = settings.momentum_threshold * volatility_20d

    if abs_change >= threshold:
        return 1.0
    elif abs_change >= threshold * 0.6:
        return 0.5
    return 0.0


def detect_oi_divergence(symbol: str, oi_change_48h_pct: float, price_change_pct: float) -> float:
    """OI 背离检测。

    OI 大变但价格没反应 = 背离信号。

    返回 0-1 分数：
    - OI 变动 > 阈值 且 价格变动 < 阈值 → 1.0
    - OI 变动 > 阈值*0.5 且 价格变动 < 阈值 → 0.5
    - 其他 → 0.0
    """
    oi_thresh = settings.oi_change_threshold
    price_thresh = settings.oi_price_divergence_threshold

    abs_oi_change = abs(oi_change_48h_pct)
    abs_price_change = abs(price_change_pct)

    if abs_oi_change >= oi_thresh and abs_price_change < price_thresh:
        return 1.0
    elif abs_oi_change >= oi_thresh * 0.5 and abs_price_change < price_thresh:
        return 0.5
    return 0.0


def compute_all_signals() -> list[dict]:
    """从 market_snapshots 读最新数据，计算所有动量信号。

    返回 [{symbol, momentum_score, oi_divergence_score, ...}, ...]
    """
    session = get_session(settings.database_url)
    results = []

    try:
        # 获取每个币种的最新快照
        # 先拿所有 distinct symbols
        from sqlalchemy import func
        subq = (
            session.query(
                MarketSnapshot.symbol,
                func.max(MarketSnapshot.captured_at).label("max_ts"),
            )
            .group_by(MarketSnapshot.symbol)
            .subquery()
        )

        snapshots = (
            session.query(MarketSnapshot)
            .join(
                subq,
                (MarketSnapshot.symbol == subq.c.symbol)
                & (MarketSnapshot.captured_at == subq.c.max_ts),
            )
            .all()
        )

        for snap in snapshots:
            momentum = detect_momentum_anomaly(
                snap.symbol, snap.price_change_pct, snap.volatility_20d
            )
            oi_div = detect_oi_divergence(
                snap.symbol, snap.oi_change_48h_pct, snap.price_change_pct
            )

            results.append({
                "symbol": snap.symbol,
                "momentum_score": momentum,
                "oi_divergence_score": oi_div,
                "price": snap.price,
                "price_change_pct": snap.price_change_pct,
                "volatility_20d": snap.volatility_20d,
                "oi": snap.oi,
                "oi_change_48h_pct": snap.oi_change_48h_pct,
            })

        log.info("momentum_signals_computed", count=len(results))

    finally:
        session.close()

    return results


if __name__ == "__main__":
    results = compute_all_signals()
    results.sort(key=lambda x: x["momentum_score"], reverse=True)
    for r in results[:10]:
        print(f'{r["symbol"]:15s} momentum={r["momentum_score"]:.1f} oi_div={r["oi_divergence_score"]:.1f} change={r["price_change_pct"]:+.2f}%')
