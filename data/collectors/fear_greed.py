"""恐惧贪婪指数采集器 — Alternative.me"""

from datetime import datetime, timezone

import httpx
import structlog

from config.settings import settings
from data.db.models import FearGreedHistory, get_session

log = structlog.get_logger()

API_URL = "https://api.alternative.me/fng/"


def fetch_and_store() -> int:
    """拉取恐贪指数。返回写入条数。"""
    session = get_session(settings.database_url)
    count = 0

    try:
        resp = httpx.get(API_URL, params={"limit": 30}, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        for item in data.get("data", []):
            value = int(item["value"])
            classification = item["value_classification"]
            ts = datetime.fromtimestamp(int(item["timestamp"]), tz=timezone.utc)

            # 跳过已存在的记录（unique timestamp）
            exists = (
                session.query(FearGreedHistory)
                .filter(FearGreedHistory.timestamp == ts)
                .first()
            )
            if exists:
                continue

            record = FearGreedHistory(
                value=value,
                classification=classification,
                timestamp=ts,
            )
            session.add(record)
            count += 1

        session.commit()
        log.info("fear_greed_saved", count=count, latest=data["data"][0] if data.get("data") else None)

    except Exception as e:
        session.rollback()
        log.error("fear_greed_collect_failed", error=str(e))
        raise
    finally:
        session.close()

    return count


if __name__ == "__main__":
    n = fetch_and_store()
    print(f"Done: {n} records saved")
