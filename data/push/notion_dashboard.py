"""Notion 数据看板推送 — 定时推送到 HUB 的 3 个 inline DB"""

import os
from datetime import datetime, timedelta

import requests
import structlog

from config.settings import settings
from data.db.models import (
    FearGreedHistory,
    MarketSnapshot,
    RiskState,
    SignalScore,
    get_session,
)

log = structlog.get_logger()

NOTION_TOKEN = os.environ.get("NOTION_API_KEY", "")
NOTION_VERSION = "2025-09-03"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

# Data source IDs (inline DBs in HUB use data_sources API)
DS_SIGNAL_RANK = "d8583fe2-1f78-4a79-b950-9ed9a9d94fc0"
DS_DAILY_MARKET = "f45fa168-8e9c-4df5-8e01-df54acb384eb"
DS_SYSTEM_STATUS = "74a5b1de-4b13-44ba-a3bf-119e2cc644b8"

# DB IDs for archiving
DB_SIGNAL_RANK = "386930d8-eb46-4645-b173-f0f55e027c00"
DB_DAILY_MARKET = "dada5583-e22e-42f9-82ae-2a65087d89fa"
DB_SYSTEM_STATUS = "4b9ed067-f135-4298-9302-44f77dc022af"


def _notion_post(url: str, payload: dict) -> dict:
    resp = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    if resp.status_code >= 400:
        log.error("notion_api_error", status=resp.status_code, body=resp.text[:200])
    return resp.json()


def _archive_old_rows(ds_id: str, keep: int = 0):
    """Archive 旧行"""
    resp = requests.post(
        f"https://api.notion.com/v1/data_sources/{ds_id}/query",
        headers=HEADERS,
        json={"page_size": 100},
        timeout=30,
    )
    rows = resp.json().get("results", [])
    to_delete = rows[:-keep] if keep > 0 else rows
    for row in to_delete:
        requests.patch(
            f"https://api.notion.com/v1/pages/{row['id']}",
            headers=HEADERS,
            json={"in_trash": True},
            timeout=10,
        )
    if to_delete:
        log.info("notion_rows_archived", ds=ds_id[:8], count=len(to_delete))


def _init_ds_schema(ds_id: str, schema: dict):
    """初始化 data_source schema（空 DB 首次写入前调用）"""
    resp = requests.patch(
        f"https://api.notion.com/v1/data_sources/{ds_id}",
        headers=HEADERS,
        json={"schema": schema},
    )
    if resp.status_code >= 400:
        log.error("schema_init_failed", ds=ds_id[:8], body=resp.text[:200])
    return resp


def push_signal_ranking():
    """推送信号排行榜 Top 10"""
    session = get_session(settings.database_url)
    try:
        from sqlalchemy import desc, func

        subq = session.query(func.max(SignalScore.captured_at).label("max_ts")).subquery()
        scores = (
            session.query(SignalScore)
            .filter(SignalScore.captured_at == subq.c.max_ts)
            .order_by(desc(SignalScore.score_total))
            .limit(10)
            .all()
        )

        if not scores:
            log.warning("no_signal_scores_to_push")
            return

        _archive_old_rows(DS_SIGNAL_RANK, keep=0)

        # Schema: 币种(title), 排名, 总分, 动量分, OI背离分, 白名单分, 策略, 价格, 涨跌幅%, 快照时间
        for rank, s in enumerate(scores, 1):
            _notion_post(
                "https://api.notion.com/v1/pages",
                {
                    "parent": {"database_id": DB_SIGNAL_RANK},
                    "properties": {
                        "币种": {"title": [{"text": {"content": s.symbol}}]},
                        "排名": {"number": rank},
                        "总分": {"number": round(s.score_total, 3)},
                        "动量分": {"number": round(s.score_momentum, 1)},
                        "OI背离分": {"number": round(s.score_oi_divergence, 1)},
                        "白名单分": {"number": round(s.score_whitelist, 1)},
                        "策略": {"select": {"name": s.strategy_type}},
                    },
                },
            )

        log.info("signal_ranking_pushed", count=len(scores))

    except Exception as e:
        log.error("signal_ranking_push_failed", error=str(e))
    finally:
        session.close()


def push_daily_market():
    """推送每日市场概况"""
    session = get_session(settings.database_url)
    try:
        from sqlalchemy import desc, func

        # Check if schema exists, init if not
        resp = requests.post(
            f"https://api.notion.com/v1/data_sources/{DS_DAILY_MARKET}/query",
            headers=HEADERS,
            json={},
        )
        if not resp.json().get("results"):
            _init_ds_schema(DS_DAILY_MARKET, {
                "日期": {"name": "日期", "type": "title", "title": {}},
                "恐贪指数": {"name": "恐贪指数", "type": "number", "number": {"format": "number"}},
                "恐贪分类": {"name": "恐贪分类", "type": "select", "select": {"options": [{"name": "Fear"}, {"name": "Greed"}, {"name": "Extreme Fear"}, {"name": "Extreme Greed"}, {"name": "Neutral"}]}},
                "BTC波动率": {"name": "BTC波动率", "type": "number", "number": {"format": "percent"}},
                "山寨BTC波动比": {"name": "山寨BTC波动比", "type": "number", "number": {"format": "number"}},
                "策略模式": {"name": "策略模式", "type": "select", "select": {"options": [{"name": "策略A（追高）"}, {"name": "策略B（多因子）"}, {"name": "已暂停"}]}},
                "Top1币种": {"name": "Top1币种", "type": "select", "select": {"options": [{"name": "N/A"}]}},
                "Top1分数": {"name": "Top1分数", "type": "number", "number": {"format": "number"}},
                "触发信号数": {"name": "触发信号数", "type": "number", "number": {"format": "number"}},
            })

        # 恐贪
        fg = session.query(FearGreedHistory).order_by(desc(FearGreedHistory.timestamp)).first()

        # BTC 波动率
        btc = session.query(MarketSnapshot).filter(MarketSnapshot.symbol == "BTC/USDT").order_by(desc(MarketSnapshot.captured_at)).first()

        # 策略权重
        from signals.strategy_switcher import get_strategy_weights
        sw = get_strategy_weights()

        # Top 1
        subq = session.query(func.max(SignalScore.captured_at).label("max_ts")).subquery()
        top1 = session.query(SignalScore).filter(SignalScore.captured_at == subq.c.max_ts).order_by(desc(SignalScore.score_total)).first()

        # 触发信号数
        triggered = session.query(SignalScore).filter(SignalScore.captured_at == subq.c.max_ts, SignalScore.score_total > 0.3).count()

        fg_value = fg.value if fg else 0
        fg_class_raw = fg.classification if fg else "Neutral"
        # 映射为中文（匹配 Notion DB 的 select options）
        fg_class_map = {
            "Extreme Fear": "极度恐惧",
            "Fear": "恐惧",
            "Neutral": "中性",
            "Greed": "贪婪",
            "Extreme Greed": "极度贪婪",
        }
        fg_class = fg_class_map.get(fg_class_raw, fg_class_raw)
        btc_vol = (btc.volatility_20d * 100) if btc else 0
        ratio = sw.get("ratio", 0)
        mode = "已暂停" if sw.get("paused") else ("策略A（追高）" if sw.get("weight_a", 0) > 0.5 else "策略B（多因子）")
        top1_sym = top1.symbol.replace("/", "-") if top1 else "N/A"
        top1_score = top1.score_total if top1 else 0

        _archive_old_rows(DS_DAILY_MARKET, keep=0)

        _notion_post(
            "https://api.notion.com/v1/pages",
            {
                "parent": {"database_id": DB_DAILY_MARKET},
                "properties": {
                    "日期": {"title": [{"text": {"content": datetime.utcnow().strftime("%Y-%m-%d")}}]},
                    "恐贪指数": {"number": fg_value},
                    "恐贪分类": {"select": {"name": fg_class}},
                    "BTC波动率": {"number": round(btc_vol, 2)},
                    "山寨/BTC波动比": {"number": round(ratio, 2)},
                    "策略模式": {"select": {"name": mode}},
                    "Top 1 币种": {"rich_text": [{"text": {"content": top1_sym}}]},
                    "Top 1 分数": {"number": round(top1_score, 3)},
                    "触发信号数": {"number": triggered},
                },
            },
        )

        log.info("daily_market_pushed")

    except Exception as e:
        log.error("daily_market_push_failed", error=str(e))
    finally:
        session.close()


def push_system_status():
    """推送系统状态"""
    session = get_session(settings.database_url)
    try:
        from sqlalchemy import func

        # Check if schema exists
        resp = requests.post(
            f"https://api.notion.com/v1/data_sources/{DS_SYSTEM_STATUS}/query",
            headers=HEADERS,
            json={},
        )
        if not resp.json().get("results"):
            _init_ds_schema(DS_SYSTEM_STATUS, {
                "时间": {"name": "时间", "type": "title", "title": {}},
                "采集器": {"name": "采集器", "type": "select", "select": {"options": [{"name": "运行中"}, {"name": "异常"}]}},
                "调度器": {"name": "调度器", "type": "select", "select": {"options": [{"name": "运行中"}, {"name": "停止"}]}},
                "24h记录数": {"name": "24h记录数", "type": "number", "number": {"format": "number"}},
                "错误率": {"name": "错误率", "type": "number", "number": {"format": "percent"}},
                "连续止损": {"name": "连续止损", "type": "number", "number": {"format": "number"}},
                "当日亏损": {"name": "当日亏损", "type": "number", "number": {"format": "number"}},
                "总回撤": {"name": "总回撤", "type": "number", "number": {"format": "percent"}},
                "已暂停": {"name": "已暂停", "type": "select", "select": {"options": [{"name": "否"}, {"name": "是"}]}},
            })

        now = datetime.utcnow()
        day_ago = now - timedelta(hours=24)

        rows_24h = session.query(MarketSnapshot).filter(MarketSnapshot.captured_at >= day_ago).count()
        latest = session.query(func.max(MarketSnapshot.captured_at)).scalar()
        collector_status = "运行中" if latest and (now - latest).total_seconds() < 600 else "异常"

        risk = session.query(RiskState).order_by(RiskState.updated_at.desc()).first()

        _archive_old_rows(DS_SYSTEM_STATUS, keep=1)

        _notion_post(
            "https://api.notion.com/v1/pages",
            {
                "parent": {"database_id": DB_SYSTEM_STATUS},
                "properties": {
                    "检查时间": {"title": [{"text": {"content": now.strftime("%H:%M UTC")}}]},
                    "采集器状态": {"select": {"name": collector_status}},
                    "调度器": {"select": {"name": "运行中"}},
                    "24h记录数": {"number": rows_24h},
                    "错误率": {"number": 0},
                    "连续止损": {"number": risk.consecutive_stops if risk else 0},
                    "当日亏损(u)": {"number": risk.daily_loss if risk else 0},
                    "总回撤%": {"number": round(risk.total_drawdown_pct, 1) if risk else 0},
                    "已暂停": {"checkbox": bool(risk and risk.is_paused)},
                },
            },
        )

        log.info("system_status_pushed", collector=collector_status, rows_24h=rows_24h)

    except Exception as e:
        log.error("system_status_push_failed", error=str(e))
    finally:
        session.close()
