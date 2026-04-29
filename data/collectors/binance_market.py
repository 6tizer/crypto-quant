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
        "timeout": 30000,  # 30s 超时，防止 OI 请求挂起
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

        # 3. 批量拉 OI 数据（先拉，后面要用）
        oi_map: dict[str, float] = {}
        for sym in top_symbols:
            try:
                oi_data = exchange.fetch_open_interest(sym)
                oi_map[sym] = float(oi_data.get("openInterestAmount", 0) or 0)
            except Exception:
                oi_map[sym] = 0.0
        log.info("oi_fetched", count=len(oi_map))

        # 4. 查 48h 前的 OI 用于算变化率
        now = datetime.now(timezone.utc)
        two_days_ago = now - timedelta(hours=48)
        prev_oi_map: dict[str, float] = {}
        for sym in top_symbols:
            base_sym = sym.replace("/USDT:USDT", "/USDT")
            row = (
                session.query(MarketSnapshot)
                .filter(
                    MarketSnapshot.symbol == base_sym,
                    MarketSnapshot.captured_at <= two_days_ago,
                )
                .order_by(desc(MarketSnapshot.captured_at))
                .first()
            )
            if row and row.oi and row.oi > 0:
                prev_oi_map[sym] = row.oi

        # 5. 拉最近 20 天 K 线（算波动率 + listed_days）
        ohlcv_cache: dict[str, list] = {}
        for sym in top_symbols:
            try:
                ohlcv = exchange.fetch_ohlcv(sym, "1d", limit=20)
                ohlcv_cache[sym] = ohlcv
            except Exception as e:
                log.warning("ohlcv_fetch_failed", symbol=sym, error=str(e))
                ohlcv_cache[sym] = []

        # 6. 尝试获取上线天数（从 market info 的 listingDate）
        listed_days_map: dict[str, int] = {}
        try:
            onboard_date_map = _fetch_listing_dates(exchange, top_symbols)
            for sym, ts in onboard_date_map.items():
                if ts > 0:
                    listed_days_map[sym] = max(1, (now.timestamp() * 1000 - ts) / 86400000)
        except Exception as e:
            log.warning("listing_date_fetch_failed", error=str(e))

        # 7. 写入数据库
        for sym, ticker, vol in top:
            change_pct = ticker.get("percentage", 0) or 0
            price = ticker.get("last", 0) or 0
            base_sym = sym.replace("/USDT:USDT", "/USDT")

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
                if candle[1] > 0:
                    move = abs(candle[4] - candle[1]) / candle[1] * 100
                    max_move = max(max_move, move)

            # 算 OI 48h 变化率
            current_oi = oi_map.get(sym, 0)
            oi_change = 0.0
            prev_oi = prev_oi_map.get(sym)
            if prev_oi and prev_oi > 0 and current_oi > 0:
                oi_change = (current_oi - prev_oi) / prev_oi * 100

            # 上线天数
            listed = listed_days_map.get(sym, 0)

            snapshot = MarketSnapshot(
                symbol=base_sym,
                price=price,
                volume_24h=vol,
                price_change_pct=change_pct,
                volatility_20d=vol_20d,
                oi=current_oi,
                oi_change_48h_pct=oi_change,
                listed_days=int(listed),
                max_daily_move_pct=max_move,
                funding_rate=ticker.get("fundingRate", 0) or 0,
                captured_at=now,
            )
            session.add(snapshot)
            count += 1

        session.commit()
        log.info("market_data_saved", count=count)

    except Exception as e:
        session.rollback()
        log.error("market_collect_failed", error=str(e))
        raise
    finally:
        session.close()

    return count


def _fetch_listing_dates(exchange: ccxt.binanceusdm, symbols: list[str]) -> dict[str, int]:
    """从币安 fapi 获取合约上线时间戳（毫秒）"""
    result = {}
    try:
        resp = exchange.fapiPublicGetExchangeInfo()
        symbols_info = {s["symbol"]: s for s in resp.get("symbols", [])}
        for sym in symbols:
            # ccxt symbol "BTC/USDT:USDT" → binance "BTCUSDT"
            base = sym.split("/")[0]
            binance_sym = base + "USDT"
            info = symbols_info.get(binance_sym, {})
            onboard_ts = info.get("onboardDate", 0)
            if onboard_ts:
                result[sym] = int(onboard_ts)
    except Exception:
        pass
    return result


if __name__ == "__main__":
    n = fetch_and_store()
    print(f"Done: {n} records saved")
