"""Crypto Quant 系统配置 — pydantic-settings 从 .env 读取"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 币安 API
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_testnet: bool = True
    binance_proxy_host: str = "127.0.0.1"
    binance_proxy_port: int = 7897

    # 数据库
    database_url: str = "sqlite:///data/crypto_quant.db"

    # 日志
    log_level: str = "INFO"

    # 采集间隔（秒）
    collect_interval_market: int = 300       # 5min
    collect_interval_onchain: int = 14400    # 4h
    collect_interval_fear_greed: int = 86400  # 1d

    # 标的范围
    top_symbols_count: int = 50

    @property
    def proxy_url(self) -> str:
        return f"http://{self.binance_proxy_host}:{self.binance_proxy_port}"

    @property
    def proxies(self) -> dict[str, str]:
        url = self.proxy_url
        return {"http": url, "https": url}

    model_config = {"env_file": "config/.env", "env_file_encoding": "utf-8"}


settings = Settings()
