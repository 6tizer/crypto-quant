"""阶段 3 Demo 验证脚本 — 自动检查 7 项验证 Checklist

用法:
    PYTHONPATH=. python scripts/verify_stage3.py
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 确保项目根目录在 path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.settings import settings
from data.db.models import (
    MarketSnapshot,
    RiskState,
    SignalScore,
    Trade,
    get_session,
)


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def check_stop_loss(session) -> dict:
    """1. 止损触发 — trades 表中 close_reason='stop_loss' 的记录"""
    trades = session.query(Trade).filter(Trade.exit_reason == "stop_loss").all()
    count = len(trades)
    details = [f"{t.symbol} PnL={t.pnl:+.2f}" for t in trades[:5]]
    return {
        "name": "止损触发",
        "passed": count > 0,
        "detail": f"{count} 次" + (f"（{', '.join(details)}）" if details else ""),
    }


def check_take_profit(session) -> dict:
    """2. 止盈阶梯触发 — exit_reason 包含 trailing/take_profit"""
    from sqlalchemy import or_

    trades = session.query(Trade).filter(
        or_(
            Trade.exit_reason.contains("trailing"),
            Trade.exit_reason.contains("take_profit"),
            Trade.exit_reason.contains("tp_"),
        )
    ).all()
    count = len(trades)
    details = [f"{t.symbol} reason={t.exit_reason}" for t in trades[:5]]
    return {
        "name": "止盈阶梯触发",
        "passed": count > 0,
        "detail": f"{count} 次" + (f"（{', '.join(details)}）" if details else ""),
    }


def check_timeout_close(session) -> dict:
    """3. 48h 兜底平仓 — exit_reason='timeout'"""
    trades = session.query(Trade).filter(Trade.exit_reason == "timeout").all()
    count = len(trades)
    return {
        "name": "48h 兜底平仓",
        "passed": count > 0,
        "detail": f"{count} 次",
    }


def check_risk_pause(session) -> dict:
    """4. 风控暂停 — risk_state 表中是否有暂停记录"""
    state = session.query(RiskState).order_by(RiskState.id.desc()).first()
    if state is None:
        return {"name": "风控暂停", "passed": False, "detail": "无风控状态记录"}
    has_pause = state.is_paused or state.consecutive_stops >= 3
    reason_parts = []
    if state.consecutive_stops > 0:
        reason_parts.append(f"连续止损={state.consecutive_stops}")
    if state.daily_loss > 0:
        reason_parts.append(f"日亏={state.daily_loss:.2f}u")
    if state.total_drawdown_pct > 0:
        reason_parts.append(f"回撤={state.total_drawdown_pct:.1f}%")
    return {
        "name": "风控暂停",
        "passed": has_pause,
        "detail": "、".join(reason_parts) if reason_parts else "无暂停记录",
    }


def check_tg_notifications() -> dict:
    """5. Telegram 通知 — 检查日志中 TG 发送记录"""
    log_path = Path("/tmp/crypto-quant.log")
    if not log_path.exists():
        return {"name": "Telegram 通知", "passed": False, "detail": "日志文件不存在"}

    try:
        content = log_path.read_text()
        count = content.count("tg_sent")
        return {
            "name": "Telegram 通知",
            "passed": count > 0,
            "detail": f"已发送 {count} 条",
        }
    except Exception as e:
        return {"name": "Telegram 通知", "passed": False, "detail": str(e)}


def check_watchdog() -> dict:
    """6. Watchdog — 检查 watchdog 是否在运行"""
    log_path = Path("/tmp/crypto-quant.log")
    if not log_path.exists():
        return {"name": "Watchdog", "passed": False, "detail": "日志文件不存在"}

    try:
        content = log_path.read_text()
        # 找最后一次 trading_cycle_done 的时间
        lines = content.strip().split("\n")
        last_cycle = None
        for line in reversed(lines):
            if "trading_cycle_done" in line:
                last_cycle = line
                break

        if last_cycle is None:
            return {"name": "Watchdog", "passed": False, "detail": "未找到交易循环记录"}

        # 检查 slow 警告
        has_slow = "trading_cycle_slow" in content
        cycle_count = content.count("trading_cycle_done")

        return {
            "name": "Watchdog",
            "passed": cycle_count > 0 and not has_slow,
            "detail": f"{cycle_count} 次循环完成" + ("，有超时警告！" if has_slow else ""),
        }
    except Exception as e:
        return {"name": "Watchdog", "passed": False, "detail": str(e)}


def check_dashboard_push(session) -> dict:
    """7. 数据看板推送 — 检查 Notion 推送记录"""
    log_path = Path("/tmp/crypto-quant.log")
    if not log_path.exists():
        return {"name": "数据看板推送", "passed": False, "detail": "日志文件不存在"}

    try:
        content = log_path.read_text()
        signal_pushed = content.count("signal_ranking_pushed")
        market_pushed = content.count("daily_market_pushed")
        status_pushed = content.count("system_status_pushed")
        total = signal_pushed + market_pushed + status_pushed

        return {
            "name": "数据看板推送",
            "passed": total > 0,
            "detail": f"信号排行={signal_pushed} 市场概况={market_pushed} 系统状态={status_pushed}",
        }
    except Exception as e:
        return {"name": "数据看板推送", "passed": False, "detail": str(e)}


def get_runtime_stats(session) -> dict:
    """系统运行统计"""
    # 总交易数
    total_trades = session.query(Trade).count()
    closed_trades = session.query(Trade).filter(Trade.exit_reason != "").count()
    open_trades = session.query(Trade).filter(Trade.exit_reason == "").count()

    # 胜率
    winning = session.query(Trade).filter(Trade.pnl > 0).count()
    win_rate = (winning / closed_trades * 100) if closed_trades > 0 else 0

    # 运行时长（基于最早和最晚 market snapshot）
    from sqlalchemy import func

    earliest = session.query(func.min(MarketSnapshot.captured_at)).scalar()
    latest = session.query(func.max(MarketSnapshot.captured_at)).scalar()
    hours = 0
    if earliest and latest:
        hours = (latest - earliest).total_seconds() / 3600

    return {
        "运行时长": f"{hours:.1f} 小时",
        "总交易笔数": total_trades,
        "已平仓": closed_trades,
        "持仓中": open_trades,
        "胜率": f"{win_rate:.0f}%",
    }


def main():
    print(f"\n{'='*50}")
    print(f"  阶段 3 Demo 验证报告")
    print(f"  生成时间: {_ts()}")
    print(f"{'='*50}\n")

    session = get_session(settings.database_url)
    try:
        # 运行统计
        stats = get_runtime_stats(session)
        print("📊 系统状态:")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        print()

        # 7 项检查
        checks = [
            check_stop_loss(session),
            check_take_profit(session),
            check_timeout_close(session),
            check_risk_pause(session),
            check_tg_notifications(),
            check_watchdog(),
            check_dashboard_push(session),
        ]

        passed = 0
        print("📋 验证 Checklist:")
        for c in checks:
            icon = "✅" if c["passed"] else "❌"
            print(f"  {icon} {c['name']} — {c['detail']}")
            if c["passed"]:
                passed += 1

        print(f"\n{'='*50}")
        print(f"  总计: {passed}/{len(checks)} 项通过")
        print(f"{'='*50}\n")

        return 0 if passed == len(checks) else 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
