"""Crypto Quant 系统配置 — pydantic-settings 从 .env 读取"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # 币安 API
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_demo_trading: bool = True
    notion_api_key: str = ""
    telegram_bot_token: str = ""
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
    blacklist_symbols: str = "INTC"  # 逗号分隔的黑名单标的

    @property
    def blacklist_symbol_set(self) -> set[str]:
        """解析逗号分隔的黑名单为 set，用于快速匹配。"""
        return {s.strip() for s in self.blacklist_symbols.split(",") if s.strip()}

    # === 交易参数（阶段 3 使用，提前定义） ===
    stop_loss_amount: float = 200.0           # 固定止损金额 (u) — 大资金后期用
    stop_loss_pct: float = 0.20               # 早期止损 = 账户净值×20%
    daily_loss_limit: float = 500.0           # 固定日亏上限 (u) — 大资金后期用
    daily_loss_limit_pct: float = 0.50        # 早期日亏上限 = 账户净值×50%
    risk_mode_threshold: float = 1000.0       # 净值 > 此值时切固定金额模式
    leverage_strategy_a: int = 5              # 策略 A 杠杆
    leverage_strategy_b: int = 3              # 策略 B 杠杆
    initial_capital: float = 100.0            # 初始资金 (u)
    entry_threshold: float = 0.50             # 入场阈值（v5回测确认）
    max_positions: int = 5                    # 同时最大持仓数
    risk_per_trade: float = 0.02                # 单笔风险占 equity 的比例

    # === 止盈阶梯参数 ===
    tp_half_breakeven_multiple: float = 1.5   # 1.5x 平半 + 保本止损
    tp_sell_75pct_multiple: float = 3.0       # 3x 平剩余 75%
    tp_clear_multiple: float = 5.0            # 5x 清仓
    force_close_hours: int = 48               # 持仓超时强制平仓 (h)
    trailing_drawdown: float = 0.4            # 峰值回撤触发 trailing stop (40%)

    # === 调度与安全开关 ===
    trading_enabled: bool = False             # 交易总开关：False 时只监控持仓，不开新仓
    watchdog_enabled: bool = True             # 轻量 watchdog：记录慢交易循环
    trading_cycle_interval: int = 300         # 交易循环间隔 (s)
    trading_cycle_timeout_seconds: int = 120  # 单次交易循环超时阈值 (s)

    # === 风控参数 ===
    max_consecutive_stops: int = 3            # 连续止损暂停阈值
    max_drawdown_pct: float = 30.0            # 总回撤暂停线 %
    fear_greed_pause_line: int = 15           # 恐贪暂停线

    # === 通知 ===
    tg_bot_token: str = ""                    # Telegram Bot Token
    tg_chat_id: str = ""                      # Telegram Chat ID

    # === 信号参数 ===
    momentum_threshold: float = 1.5           # 涨幅异动倍数 (N 倍 20 日标准差)
    oi_change_threshold: float = 15.0         # OI 48h 变动阈值 %
    oi_price_divergence_threshold: float = 3.0  # OI 背离价格变动阈值 %
    whitelist_listed_days: int = 180          # 新币白名单天数
    whitelist_max_daily_move: float = 30.0    # 历史大波动阈值 %
    strategy_switch_threshold: float = 1.5    # 策略切换阈值

    # === 信号权重（策略 A） ===
    weight_a_momentum: float = 0.54
    weight_a_square_heat: float = 0.0        # 阶段 5 才有数据
    weight_a_oi_divergence: float = 0.31
    weight_a_whitelist: float = 0.15
    weight_a_kronos: float = 0.0             # 阶段 5 后补

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
