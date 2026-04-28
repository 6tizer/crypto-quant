"""标的池白名单筛选"""

import structlog

from config.settings import settings
from data.db.models import MarketSnapshot, get_session

log = structlog.get_logger()


def check_whitelist(symbol: str, listed_days: int, max_daily_move_pct: float) -> float:
    """检查币种是否在标的池白名单中。

    规则（来自 lana 第 5 条）：
    - 上线 < 180 天 → 1.0（新币，优先）
    - 历史最大单日波动 > 30% → 0.8（大波动币）
    - 两者都满足 → 1.0
    - 都不满足 → 0.0

    Returns:
        0-1 分数
    """
    is_new = listed_days > 0 and listed_days < settings.whitelist_listed_days
    is_volatile = max_daily_move_pct >= settings.whitelist_max_daily_move

    if is_new and is_volatile:
        return 1.0
    elif is_new:
        return 1.0
    elif is_volatile:
        return 0.8
    return 0.0


def get_whitelist_symbols() -> list[dict]:
    """获取白名单中的所有币种及其得分。

    Returns:
        [{symbol, whitelist_score, listed_days, max_daily_move}, ...]
    """
    session = get_session(settings.database_url)
    results = []

    try:
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
            score = check_whitelist(snap.symbol, snap.listed_days or 0, snap.max_daily_move_pct or 0)
            if score > 0:
                results.append({
                    "symbol": snap.symbol,
                    "whitelist_score": score,
                    "listed_days": snap.listed_days,
                    "max_daily_move_pct": snap.max_daily_move_pct,
                })

        log.info("whitelist_computed", in_pool=len(results), total=len(snapshots))

    finally:
        session.close()

    return results


if __name__ == "__main__":
    results = get_whitelist_symbols()
    results.sort(key=lambda x: x["whitelist_score"], reverse=True)
    for r in results:
        print(f'{r["symbol"]:15s} score={r["whitelist_score"]:.1f} listed={r["listed_days"]}d max_move={r["max_daily_move_pct"]:.1f}%')
