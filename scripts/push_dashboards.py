"""手动触发 Notion 看板推送

用法:
    PYTHONPATH=. python scripts/push_dashboards.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import structlog

from data.push.notion_dashboard import (
    push_daily_market,
    push_signal_ranking,
    push_system_status,
)

log = structlog.get_logger()


def main() -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"\n📤 Notion 看板推送 — {now}\n")

    ok = True

    try:
        push_signal_ranking()
        print("  ✅ 信号排行榜推送完成")
    except Exception as e:
        ok = False
        log.error("push_signal_ranking_failed", error=str(e))
        print(f"  ❌ 信号排行榜推送失败: {e}")

    try:
        push_daily_market()
        print("  ✅ 每日市场概况推送完成")
    except Exception as e:
        ok = False
        log.error("push_daily_market_failed", error=str(e))
        print(f"  ❌ 每日市场概况推送失败: {e}")

    try:
        push_system_status()
        print("  ✅ 系统状态推送完成")
    except Exception as e:
        ok = False
        log.error("push_system_status_failed", error=str(e))
        print(f"  ❌ 系统状态推送失败: {e}")

    print(f"\n  总状态: {'成功' if ok else '部分失败'}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
