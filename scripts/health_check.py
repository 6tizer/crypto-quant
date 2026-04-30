"""定时健康检查 — 每 30 分钟检查系统状态，异常时推送 TG 告警

由 APScheduler 注册，随 main.py 自动运行。
也可单独执行: PYTHONPATH=. python scripts/health_check.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import structlog

from config.settings import settings
from data.db.models import MarketSnapshot, RiskState, Trade, get_session

log = structlog.get_logger()

LOG_PATH = Path("/tmp/crypto-quant-stdout.log")

# 健康检查阈值
STALE_DATA_SECONDS = 600  # 10 分钟无新数据 = 异常
HEARTBEAT_STALE_SECONDS = 300  # 5 分钟无交易循环 = 异常


def _check_scheduler_alive() -> dict:
    """APScheduler 是否在运行（检查最近一次交易循环时间）"""
    if not LOG_PATH.exists():
        return {"ok": False, "name": "调度器", "detail": "日志文件不存在"}

    content = LOG_PATH.read_text()
    lines = content.strip().split("\n")

    last_cycle_time = None
    for line in reversed(lines):
        if "trading_cycle_done" in line:
            try:
                import json

                entry = json.loads(line)
                ts_str = entry.get("timestamp", "")
                last_cycle_time = datetime.fromisoformat(ts_str)
            except Exception:
                pass
            break

    if last_cycle_time is None:
        return {"ok": False, "name": "调度器", "detail": "未找到交易循环记录"}

    elapsed = (datetime.now(timezone.utc) - last_cycle_time.replace(tzinfo=timezone.utc)).total_seconds()
    if elapsed > settings.trading_cycle_interval + STALE_DATA_SECONDS:
        return {
            "ok": False,
            "name": "调度器",
            "detail": f"最后一次循环 {elapsed:.0f}s 前（超过阈值 {settings.trading_cycle_interval + STALE_DATA_SECONDS}s）",
        }

    return {"ok": True, "name": "调度器", "detail": f"正常（{elapsed:.0f}s 前）"}


def _check_data_freshness(session) -> dict:
    """最近一次数据采集是否成功"""
    from sqlalchemy import func

    latest = session.query(func.max(MarketSnapshot.captured_at)).scalar()
    if latest is None:
        return {"ok": False, "name": "数据采集", "detail": "无采集记录"}

    latest_utc = latest.replace(tzinfo=timezone.utc) if latest.tzinfo is None else latest
    elapsed = (datetime.now(timezone.utc) - latest_utc).total_seconds()
    if elapsed > STALE_DATA_SECONDS:
        return {
            "ok": False,
            "name": "数据采集",
            "detail": f"数据过期 {elapsed:.0f}s（阈值 {STALE_DATA_SECONDS}s）",
        }

    return {"ok": True, "name": "数据采集", "detail": f"正常（{elapsed:.0f}s 前）"}


def _check_database(session) -> dict:
    """SQLite 数据库是否可读写"""
    try:
        count = session.query(MarketSnapshot).limit(1).count()
        return {"ok": True, "name": "数据库", "detail": f"可读写（{count} 条记录）"}
    except Exception as e:
        return {"ok": False, "name": "数据库", "detail": str(e)}


def _check_positions(session) -> dict:
    """当前持仓状态摘要"""
    open_trades = session.query(Trade).filter(Trade.exit_reason == "").all()
    if not open_trades:
        return {"ok": True, "name": "持仓", "detail": "无持仓"}

    symbols = [t.symbol for t in open_trades]
    return {"ok": True, "name": "持仓", "detail": f"{len(open_trades)} 个持仓: {', '.join(symbols)}"}


def _check_process_alive() -> dict:
    """main.py 进程是否在运行"""
    import subprocess

    result = subprocess.run(["pgrep", "-f", "main.py"], capture_output=True, text=True)
    if result.returncode == 0:
        pids = result.stdout.strip().split("\n")
        return {"ok": True, "name": "进程", "detail": f"运行中 (PID: {', '.join(pids)})"}
    return {"ok": False, "name": "进程", "detail": "main.py 未运行"}


def run_health_check(send_alert: bool = True) -> list[dict]:
    """执行所有检查，返回结果列表。异常时推送 TG。"""
    checks = []

    # 进程检查（不需要 DB）
    checks.append(_check_process_alive())
    checks.append(_check_scheduler_alive())

    # DB 相关检查
    session = get_session(settings.database_url)
    try:
        checks.append(_check_database(session))
        checks.append(_check_data_freshness(session))
        checks.append(_check_positions(session))
    finally:
        session.close()

    # 记录日志
    all_ok = all(c["ok"] for c in checks)
    if all_ok:
        log.info("health_check_ok", checks=[c["name"] for c in checks])
    else:
        failed = [c for c in checks if not c["ok"]]
        log.error("health_check_failed", failed=[c["name"] for c in failed])

        if send_alert:
            _send_alert(failed)

    # 更新 Notion 系统状态看板（加超时保护，避免卡住调度器）
    try:
        from data.push.notion_dashboard import push_system_status

        import threading

        def _push():
            push_system_status()

        t = threading.Thread(target=_push, daemon=True)
        t.start()
        t.join(timeout=15)
        if t.is_alive():
            log.warning("dashboard_push_timeout")
    except Exception as e:
        log.warning("dashboard_push_failed", error=str(e))

    return checks


def _send_alert(failed_checks: list[dict]) -> None:
    """推送 TG 告警"""
    from notifications.tg import send_notification

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    details = "\n".join(f"  • {c['name']}: {c['detail']}" for c in failed_checks)
    msg = (
        f"⚠️ 系统健康检查异常\n"
        f"时间: {now}\n"
        f"异常项:\n{details}\n\n"
        f"建议: 检查 /tmp/crypto-quant-stdout.log"
    )
    send_notification(msg)


if __name__ == "__main__":
    results = run_health_check(send_alert=False)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n🏥 健康检查 — {now}\n")
    for c in results:
        icon = "✅" if c["ok"] else "❌"
        print(f"  {icon} {c['name']}: {c['detail']}")
    all_ok = all(c["ok"] for c in results)
    print(f"\n  总状态: {'正常' if all_ok else '异常'}\n")
    sys.exit(0 if all_ok else 1)
