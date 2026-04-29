"""历史数据下载器 — 下载 Top 50 币种 3 个月 5min K 线"""

import time
from datetime import datetime, timedelta, timezone

import ccxt
import pandas as pd
import structlog

from config.settings import settings

log = structlog.get_logger()

# 存储目录
DATA_DIR = "data/backtest"


def get_exchange() -> ccxt.binanceusdm:
    return ccxt.binanceusdm({
        "enableRateLimit": True,
        "proxies": settings.proxies,
        "options": {"defaultType": "future"},
        "timeout": 30000,
    })


def download_klines(symbol: str, timeframe: str = "5m", days: int = 90) -> pd.DataFrame:
    """下载单个币种的 K 线数据。

    Returns:
        DataFrame with columns: timestamp, open, high, low, close, volume
    """
    exchange = get_exchange()
    since = exchange.parse8601(
        (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    )
    all_ohlcv = []
    limit = 1000  # Binance max per request

    while True:
        try:
            ohlcv = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        except Exception as e:
            log.warning("download_failed", symbol=symbol, error=str(e))
            break

        if not ohlcv:
            break

        all_ohlcv.extend(ohlcv)
        last_ts = ohlcv[-1][0]
        since = last_ts + 1  # next candle after last

        if len(ohlcv) < limit:
            break  # no more data

        time.sleep(0.2)  # rate limit

    if not all_ohlcv:
        return pd.DataFrame()

    df = pd.DataFrame(all_ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    return df


def download_top_symbols(top_n: int = 50, days: int = 90) -> dict[str, pd.DataFrame]:
    """下载 Top N 币种的 K 线数据。

    Returns:
        {symbol: DataFrame}
    """
    import os
    os.makedirs(DATA_DIR, exist_ok=True)

    exchange = get_exchange()
    exchange.load_markets()

    # 获取 USDT 永续合约
    symbols = [
        sym for sym, info in exchange.markets.items()
        if info.get("quote") == "USDT" and info.get("linear") and info.get("active") and info.get("swap")
    ]

    tickers = exchange.fetch_tickers()
    ranked = sorted(
        [(sym, tickers.get(sym, {}).get("quoteVolume", 0) or 0) for sym in symbols],
        key=lambda x: x[1],
        reverse=True,
    )
    top = [sym for sym, _ in ranked[:top_n]]

    # 排除非加密货币
    exclude = {"XAU/USDT", "XAG/USDT", "PAXG/USDT", "CL/USDT", "TSLA/USDT"}
    top = [sym for sym in top if sym not in exclude]

    log.info("download_start", total=len(top), days=days)

    results = {}
    for i, sym in enumerate(top):
        # 缓存文件
        cache_file = f"{DATA_DIR}/{sym.replace('/', '_').replace(':', '')}_{days}d_5m.parquet"
        # Try different timeframe patterns in cache
        import glob
        cache_pattern = f"{DATA_DIR}/{sym.replace('/', '_').replace(':', '')}_{days}d_*.parquet"
        existing = glob.glob(cache_pattern)

        if existing:
            df = pd.read_parquet(existing[0])
            results[sym] = df
            log.info("cache_hit", symbol=sym, rows=len(df))
            continue

        df = download_klines(sym, "5m", days)
        if len(df) > 0:
            df.to_parquet(cache_file, index=False)
            results[sym] = df
            log.info("downloaded", symbol=sym, rows=len(df), progress=f"{i+1}/{len(top)}")
        else:
            log.warning("no_data", symbol=sym)

        time.sleep(0.5)

    log.info("download_complete", symbols=len(results))
    return results


if __name__ == "__main__":
    data = download_top_symbols(top_n=50, days=90)
    for sym, df in list(data.items())[:5]:
        print(f"{sym}: {len(df)} rows, {df['timestamp'].min()} ~ {df['timestamp'].max()}")
    print(f"\nTotal: {len(data)} symbols")
