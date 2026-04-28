"""验证脚本 — 运行一次所有采集器，检查数据落盘"""

import sys
import structlog

from config.settings import settings
from data.db.models import (
    MarketSnapshot,
    OnchainMetric,
    FearGreedHistory,
    get_session,
    init_db,
)

structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger("INFO"),
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)
log = structlog.get_logger()


def verify() -> bool:
    init_db(settings.database_url)
    all_ok = True

    # 1. 行情数据
    log.info("=== 验证行情数据 ===")
    try:
        from data.collectors.binance_market import fetch_and_store
        n = fetch_and_store()
        session = get_session(settings.database_url)
        count = session.query(MarketSnapshot).count()
        log.info("market_snapshots", written=n, total=count)
        if count > 0:
            top3 = session.query(MarketSnapshot).order_by(MarketSnapshot.volume_24h.desc()).limit(3).all()
            for s in top3:
                log.info("  top_symbol", symbol=s.symbol, price=s.price, vol_24h=s.volume_24h, change=f"{s.price_change_pct:+.2f}%")
        else:
            log.error("NO MARKET DATA")
            all_ok = False
        session.close()
    except Exception as e:
        log.error("market_verify_failed", error=str(e))
        all_ok = False

    # 2. 链上数据
    log.info("=== 验证链上数据 ===")
    try:
        from data.collectors.onchain import fetch_and_store
        n = fetch_and_store()
        session = get_session(settings.database_url)
        count = session.query(OnchainMetric).count()
        log.info("onchain_metrics", written=n, total=count)
        session.close()
    except Exception as e:
        log.error("onchain_verify_failed", error=str(e))

    # 3. 恐贪指数
    log.info("=== 验证恐贪指数 ===")
    try:
        from data.collectors.fear_greed import fetch_and_store
        n = fetch_and_store()
        session = get_session(settings.database_url)
        latest = session.query(FearGreedHistory).order_by(FearGreedHistory.timestamp.desc()).first()
        if latest:
            log.info("latest_fear_greed", value=latest.value, classification=latest.classification)
        else:
            log.error("NO FEAR/GREED DATA")
            all_ok = False
        session.close()
    except Exception as e:
        log.error("fear_greed_verify_failed", error=str(e))
        all_ok = False

    if all_ok:
        log.info("=== ✅ ALL CHECKS PASSED ===")
    else:
        log.error("=== ❌ SOME CHECKS FAILED ===")

    return all_ok


if __name__ == "__main__":
    ok = verify()
    sys.exit(0 if ok else 1)
