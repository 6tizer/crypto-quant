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


def run_signal_scorer() -> None:
    """定时信号打分 + 推送排行榜"""
    log.info("signal_scorer_start")
    try:
        from signals.scorer import score_all_symbols
        results = score_all_symbols()
        log.info("signal_scorer_done", count=len(results), top3=[r["symbol"] for r in results[:3]])
    except Exception as e:
        log.error("signal_scorer_error", error=str(e))


def run_push_signal_ranking() -> None:
    """推送信号排行榜到 Notion"""
    try:
        from data.push.notion_dashboard import push_signal_ranking
        push_signal_ranking()
    except Exception as e:
        log.error("push_signal_ranking_error", error=str(e))


def run_push_daily_market() -> None:
    """推送每日市场概况到 Notion"""
    try:
        from data.push.notion_dashboard import push_daily_market
        push_daily_market()
    except Exception as e:
        log.error("push_daily_market_error", error=str(e))


def run_push_system_status() -> None:
    """推送系统状态到 Notion"""
    try:
        from data.push.notion_dashboard import push_system_status
        push_system_status()
    except Exception as e:
        log.error("push_system_status_error", error=str(e))


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

    # 信号打分（每 5min，跟行情采集同步）
    scheduler.add_job(
        run_signal_scorer,
        "interval",
        seconds=settings.collect_interval_market,
        id="signal_scorer",
        name="信号打分",
    )

    # Notion 看板推送
    scheduler.add_job(
        run_push_signal_ranking,
        "interval",
        minutes=60,
        id="push_signal_ranking",
        name="推送信号排行榜",
    )
    scheduler.add_job(
        run_push_daily_market,
        "interval",
        hours=24,
        id="push_daily_market",
        name="推送每日市场概况",
    )
    scheduler.add_job(
        run_push_system_status,
        "interval",
        minutes=60,
        id="push_system_status",
        name="推送系统状态",
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
