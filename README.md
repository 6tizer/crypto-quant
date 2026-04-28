# Crypto Quant — 加密量化交易系统

> 程序 × AI × 人 三层分工架构

## 项目概述

加密货币量化交易系统，采用三层分工：
- **程序层**（Python 24/7）：数据采集、信号计算、交易执行、风控
- **AI 层**（Hermes Skill）：市场环境判断、异常诊断、复盘分析
- **人**：审批、决策、资金管理

当前阶段：**阶段 1（基础设施 + 数据采集）**

## 快速开始

```bash
# 安装依赖（需要 uv）
uv sync

# 验证（运行一次所有采集器）
PYTHONPATH=. uv run python scripts/verify.py

# 启动调度服务
PYTHONPATH=. uv run python main.py
```

## 技术栈

- Python 3.12+, uv 包管理
- SQLite（开发）→ PostgreSQL（生产）
- APScheduler 调度
- ccxt（币安合约 API）
- httpx（异步 HTTP）
- structlog（JSON 日志）
- pydantic-settings（配置管理）

## 目录结构

```
crypto-quant/
├── config/           # 配置（settings.py + .env）
├── data/
│   ├── collectors/   # 数据采集器（binance_market / onchain / fear_greed）
│   └── db/           # 数据库模型（SQLAlchemy）
├── signals/          # 信号计算（阶段 2）
├── execution/        # 交易执行（阶段 3）
├── backtest/         # 回测框架（阶段 2.5）
├── hermes/           # Hermes AI Skill（阶段 4）
├── social/           # 社交发布（阶段 5）
├── main.py           # 主入口
└── scripts/verify.py # 验证脚本
```

## 数据源

| 数据源 | 频率 | 内容 |
|--------|------|------|
| 币安合约 API | 5min | 行情、OI、资金费率 |
| DeFiLlama | 4h | TVL |
| Alternative.me | 每日 | 恐惧贪婪指数 |

## 注意事项

- 国内环境币安 API 需要代理（默认 127.0.0.1:7897）
- 阶段 1-2 使用 testnet，不需要 API Key
- 日志统一 structlog JSON 格式
