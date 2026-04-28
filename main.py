"""Crypto Quant 主入口 — APScheduler 调度"""

import signal
import sys
from datetime import datetime, timezone

import structlog
from apscheduler.schedulers.background import BackgroundScheduler

from config.settings import settings
from data.db.models import init_db

log = structlog.get_logger()


def setup_logging() -> None:
    """配置 structlog JSON 输出"""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(settings.log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def run_market_collector() -> None:
    log.info("market_collector_start")
    try:
        from data.collectors.binance_market import fetch_and_store
        n = fetch_and_store()
        log.info("market_collector_done", records=n)
    except Exception as e:
        log.error("market_collector_error", error=str(e))


def run_onchain_collector() -> None:
    log.info("onchain_collector_start")
    try:
        from data.collectors.onchain import fetch_and_store
        n = fetch_and_store()
        log.info("onchain_collector_done", records=n)
    except Exception as e:
        log.error("onchain_collector_error", error=str(e))


def run_fear_greed_collector() -> None:
    log.info("fear_greed_collector_start")
    try:
        from data.collectors.fear_greed import fetch_and_store
        n = fetch_and_store()
        log.info("fear_greed_collector_done", records=n)
    except Exception as e:
        log.error("fear_greed_collector_error", error=str(e))


def main() -> None:
    setup_logging()
    log.info("crypto_quant_starting", settings={
        "db": settings.database_url,
        "proxy": settings.proxy_url,
        "intervals": {
            "market": settings.collect_interval_market,
            "onchain": settings.collect_interval_onchain,
            "fear_greed": settings.collect_interval_fear_greed,
        },
    })

    # 初始化数据库
    init_db(settings.database_url)
    log.info("database_initialized")

    # 创建调度器
    scheduler = BackgroundScheduler()

    # 注册采集任务
    scheduler.add_job(
        run_market_collector,
        "interval",
        seconds=settings.collect_interval_market,
        id="market_collector",
        name="行情数据采集",
    )
    scheduler.add_job(
        run_onchain_collector,
        "interval",
        seconds=settings.collect_interval_onchain,
        id="onchain_collector",
        name="链上数据采集",
    )
    scheduler.add_job(
        run_fear_greed_collector,
        "interval",
        seconds=settings.collect_interval_fear_greed,
        id="fear_greed_collector",
        name="恐贪指数采集",
    )

    # 启动时立即跑一次
    log.info("running_initial_collection")
    run_market_collector()
    run_onchain_collector()
    run_fear_greed_collector()
    log.info("initial_collection_done")

    # 优雅关闭
    def shutdown(signum, frame):
        log.info("shutdown_signal", signal=signum)
        scheduler.shutdown(wait=False)
        log.info("crypto_quant_stopped")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # 启动调度器
    scheduler.start()
    log.info("scheduler_started")

    # 保持进程运行
    try:
        signal.pause()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
