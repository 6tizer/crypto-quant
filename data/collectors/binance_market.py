"""币安合约行情采集器 — ccxt + 代理"""

import statistics
from datetime import datetime, timedelta, timezone

import ccxt
import structlog
from sqlalchemy import desc

from config.settings import settings
from data.db.models import MarketSnapshot, get_session

log = structlog.get_logger()


def get_exchange() -> ccxt.binanceusdm:
    """创建配置好代理的交易所实例"""
    return ccxt.binanceusdm({
        "enableRateLimit": True,
        "proxies": settings.proxies,
        "options": {"defaultType": "future"},
    })


def fetch_and_store() -> int:
    """拉取行情数据并存入数据库。返回写入条数。"""
    exchange = get_exchange()
    session = get_session(settings.database_url)
    count = 0

    try:
        # 1. 拉所有 USDT 永续合约 tickers
        exchange.load_markets()
        symbols = [
            sym for sym, info in exchange.markets.items()
            if info.get("quote") == "USDT"
            and info.get("linear")
            and info.get("active")
            and info.get("swap")
        ]
        log.info("markets_loaded", total=len(symbols))

        tickers = exchange.fetch_tickers()
        log.info("tickers_fetched", count=len(tickers))

        # 2. 按 24h 成交量排序取 Top N
        crypto_tickers = []
        for sym in symbols:
            t = tickers.get(sym, {})
            vol = t.get("quoteVolume", 0) or 0
            if vol > 0:
                crypto_tickers.append((sym, t, vol))

        crypto_tickers.sort(key=lambda x: x[2], reverse=True)
        top = crypto_tickers[: settings.top_symbols_count]
        top_symbols = [sym for sym, _, _ in top]
        log.info("top_symbols_selected", count=len(top_symbols))

        # 3. 拉最近 20 天 K 线（用于算波动率）
        ohlcv_cache: dict[str, list] = {}
        for sym in top_symbols:
            try:
                ohlcv = exchange.fetch_ohlcv(sym, "1d", limit=20)
                ohlcv_cache[sym] = ohlcv
            except Exception as e:
                log.warning("ohlcv_fetch_failed", symbol=sym, error=str(e))
                ohlcv_cache[sym] = []

        # 4. 写入数据库
        now = datetime.now(timezone.utc)
        for sym, ticker, vol in top:
            change_pct = ticker.get("percentage", 0) or 0
            price = ticker.get("last", 0) or 0

            # 算 20 日波动率
            daily_returns = []
            ohlcv = ohlcv_cache.get(sym, [])
            for i in range(1, len(ohlcv)):
                prev_close = ohlcv[i - 1][4]
                curr_close = ohlcv[i][4]
                if prev_close > 0:
                    daily_returns.append((curr_close - prev_close) / prev_close)

            vol_20d = statistics.stdev(daily_returns) if len(daily_returns) >= 2 else 0

            # 算历史最大单日波动
            max_move = 0
            for candle in ohlcv:
                if candle[1] > 0:  # open > 0
                    move = abs(candle[4] - candle[1]) / candle[1] * 100
                    max_move = max(max_move, move)

            snapshot = MarketSnapshot(
                symbol=sym.replace("/USDT:USDT", "/USDT"),
                price=price,
                volume_24h=vol,
                price_change_pct=change_pct,
                volatility_20d=vol_20d,
                oi=0,  # OI 需要单独接口，暂时留 0
                oi_change_48h_pct=0,
                max_daily_move_pct=max_move,
                funding_rate=ticker.get("fundingRate", 0) or 0,
                captured_at=now,
            )
            session.merge(snapshot)
            count += 1

        session.commit()
        log.info("market_data_saved", count=count)

        # 5. 尝试拉 OI 数据（币安 fapi 公共接口）
        _fetch_oi(exchange, session, top_symbols)

    except Exception as e:
        session.rollback()
        log.error("market_collect_failed", error=str(e))
        raise
    finally:
        session.close()

    return count


def _fetch_oi(exchange: ccxt.binanceusdm, session, symbols: list[str]) -> None:
    """拉取 OI 数据并更新 market_snapshots"""
    count = 0
    now = datetime.now(timezone.utc)

    for sym in symbols:
        try:
            # ccxt 统一接口
            oi_data = exchange.fetch_open_interest(sym)
            if oi_data and oi_data.get("openInterestAmount"):
                base_sym = sym.replace("/USDT:USDT", "/USDT")
                # 找最新一条记录更新 OI
                latest = (
                    session.query(MarketSnapshot)
                    .filter(MarketSnapshot.symbol == base_sym)
                    .order_by(desc(MarketSnapshot.captured_at))
                    .first()
                )
                if latest:
                    latest.oi = oi_data["openInterestAmount"]
                    count += 1
        except Exception:
            pass  # OI 接口不是所有币都有，静默跳过

    if count > 0:
        session.commit()
        log.info("oi_data_updated", count=count)


if __name__ == "__main__":
    # 单独测试
    n = fetch_and_store()
    print(f"Done: {n} records saved")
