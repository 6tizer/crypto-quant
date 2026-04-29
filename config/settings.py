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

    # === 交易参数（阶段 3 使用，提前定义） ===
    stop_loss_amount: float = 200.0           # 固定止损金额 (u) — 大资金后期用
    stop_loss_pct: float = 0.20               # 早期止损 = 账户净值×20%
    daily_loss_limit: float = 500.0           # 固定日亏上限 (u) — 大资金后期用
    daily_loss_limit_pct: float = 0.50        # 早期日亏上限 = 账户净值×50%
    risk_mode_threshold: float = 1000.0       # 净值 > 此值时切固定金额模式
    leverage_strategy_a: int = 5              # 策略 A 杠杆
    leverage_strategy_b: int = 3              # 策略 B 杠杆
    initial_capital: float = 100.0            # 初始资金 (u)
    entry_threshold: float = 0.65             # 入场阈值

    # === 风控参数 ===
    max_consecutive_stops: int = 3            # 连续止损暂停阈值
    max_drawdown_pct: float = 30.0            # 总回撤暂停线 %
    fear_greed_pause_line: int = 15           # 恐贪暂停线

    # === 信号参数 ===
    momentum_threshold: float = 2.5           # 涨幅异动倍数 (N 倍 20 日标准差)
    oi_change_threshold: float = 15.0         # OI 48h 变动阈值 %
    oi_price_divergence_threshold: float = 3.0  # OI 背离价格变动阈值 %
    whitelist_listed_days: int = 180          # 新币白名单天数
    whitelist_max_daily_move: float = 30.0    # 历史大波动阈值 %
    strategy_switch_threshold: float = 1.5    # 策略切换阈值

    # === 信号权重（策略 A） ===
    weight_a_momentum: float = 0.35
    weight_a_square_heat: float = 0.25        # 阶段 5 才有数据
    weight_a_oi_divergence: float = 0.20
    weight_a_whitelist: float = 0.10
    weight_a_kronos: float = 0.10             # 阶段 5 后补

    # === 信号权重（策略 B） ===
    weight_b_kronos: float = 0.25
    weight_b_mvrv: float = 0.20
    weight_b_sopr: float = 0.15
    weight_b_etf: float = 0.15
    weight_b_smart_money: float = 0.15
    weight_b_fear_greed: float = 0.10

    @property
    def proxy_url(self) -> str:
        return f"http://{self.binance_proxy_host}:{self.binance_proxy_port}"

    @property
    def proxies(self) -> dict[str, str]:
        url = self.proxy_url
        return {"http": url, "https": url}

    model_config = {"env_file": "config/.env", "env_file_encoding": "utf-8"}


settings = Settings()
