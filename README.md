# Crypto Quant — 加密量化交易系统

> 程序 × AI × 人 三层分工架构

## 项目概述

加密货币量化交易系统，采用三层分工：
- **程序层**（Python 24/7）：数据采集 → 信号打分 → 交易执行 → 风控 → 持仓监控
- **AI 层**（Hermes Skill）：市场环境判断、异常诊断、复盘分析（阶段 4）
- **人**：审批决策、资金管理

当前阶段：**阶段 3（模拟盘联调）** — 交易执行 + 风控已完成，Demo 环境运行中

## 快速开始

```bash
# 安装依赖（需要 uv）
uv sync

# 验证（运行一次所有采集器）
PYTHONPATH=. uv run python scripts/verify.py

# 启动调度服务（常驻）
PYTHONPATH=. uv run python main.py

# 回测
PYTHONPATH=. uv run python -m backtest.simple_backtest
```

## 技术栈

- Python 3.12+, uv 包管理
- SQLite（开发）→ PostgreSQL（生产）
- APScheduler 定时调度
- ccxt（币安 U 本位合约 API）
- httpx / requests（HTTP 客户端）
- structlog（JSON 日志，统一用 `structlog.get_logger()`）
- pydantic-settings（配置管理，从 `.env` 读取）

## 目录结构

```
crypto-quant/
├── config/               # 配置
│   ├── settings.py       # pydantic-settings，所有参数定义
│   └── .env              # 密钥 + 参数（不入 git）
├── data/
│   ├── collectors/       # 数据采集器（binance_market / onchain / fear_greed）
│   ├── db/               # SQLAlchemy 模型（MarketSnapshot, SignalScore, Trade, RiskState）
│   └── push/             # Notion 看板推送
├── signals/              # 信号计算 + 打分引擎（策略 A 加权 + 策略 B 链上指标）
├── execution/            # 交易执行
│   ├── order_manager.py  # 开仓（ccxt 市价多 + 止损联动）
│   ├── position_monitor.py # 持仓监控 + 阶梯止盈 + trailing stop
│   ├── portfolio.py      # 仓位计算 + 账户余额
│   └── risk_guard.py     # 风控（连续止损 / 日亏 / 总回撤 / 恐贪）
├── notifications/        # Telegram 通知
├── utils/                # 工具（符号转换 symbol.py）
├── backtest/             # 回测框架
├── main.py               # 主入口，APScheduler 注册所有定时任务
└── pyproject.toml
```

## 数据源

- **币安合约 API**（5min）— 行情、OI、资金费率、波动率
- **DeFiLlama**（4h）— TVL
- **Alternative.me**（每日）— 恐惧贪婪指数

## 交易参数（当前）

- 入场阈值：0.50
- 杠杆：策略 A 5x / 策略 B 3x
- 止损：ATR 动态计算
- 止盈阶梯：1.5x 平半+保本 → 3x 平 75% → 5x 清仓 → 48h 强平
- Trailing stop：峰值回撤 40%
- 风控四条线：连续止损 3 次 / 日亏上限 / 总回撤 30% / 恐贪 < 15

## 注意事项

- 国内环境币安 API 需走代理 `http://127.0.0.1:7897`
- Demo 环境不支持任何条件单（STOP/STOP_MARKET），止损靠 Python 端轮询
- 阶段 1-3 用 Demo Trading（testnet），不需要真实资金
- 日志统一 structlog JSON 格式，不要用 print
- 所有配置参数在 `config/settings.py` 定义，不硬编码
