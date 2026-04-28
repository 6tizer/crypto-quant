"""链上数据采集器 — DeFiLlama TVL"""

from datetime import datetime, timezone

import httpx
import structlog

from config.settings import settings
from data.db.models import OnchainMetric, get_session

log = structlog.get_logger()

# DeFiLlama 公开 API，无需认证，无需代理
DEFILLAMA_ENDPOINTS = {
    "tvl_dex": "https://api.llama.fi/overview/dexs",
    "tvl_total": "https://api.llama.fi/chains",
}


def fetch_and_store() -> int:
    """拉取 DeFiLlama TVL 数据。返回写入条数。"""
    session = get_session(settings.database_url)
    count = 0

    try:
        for name, url in DEFILLAMA_ENDPOINTS.items():
            try:
                resp = httpx.get(url, timeout=30)
                resp.raise_for_status()
                data = resp.json()

                if name == "tvl_total":
                    # /chains 返回列表，取 Ethereum 的 TVL
                    eth = next((c for c in data if c.get("name") == "Ethereum"), {})
                    tvl = eth.get("tvl", 0)
                else:
                    # /overview/dexs 等
                    tvl = data.get("total24h") or 0

                metric = OnchainMetric(
                    metric_name=f"defillama_{name}",
                    value=float(tvl) if tvl else 0,
                    captured_at=datetime.now(timezone.utc),
                )
                session.add(metric)
                count += 1
                log.info("onchain_data_fetched", metric=name, value=tvl)

            except Exception as e:
                log.warning("onchain_fetch_failed", metric=name, error=str(e))

        session.commit()
        log.info("onchain_data_saved", count=count)

    except Exception as e:
        session.rollback()
        log.error("onchain_collect_failed", error=str(e))
        raise
    finally:
        session.close()

    return count


if __name__ == "__main__":
    n = fetch_and_store()
    print(f"Done: {n} records saved")
