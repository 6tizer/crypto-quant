# Crypto Quant — 加密量化交易系统

## 项目概述
三层分工架构：程序层（Python 24/7）× AI 层（Hermes Skill）× 人（审批决策）
当前阶段：阶段 1 — 基础设施 + 数据采集

## 技术栈
- Python 3.12+, uv 包管理
- SQLite（开发）→ PostgreSQL（生产）
- APScheduler 调度
- ccxt（币安合约 API）
- httpx（异步 HTTP）
- structlog（JSON 日志）
- pydantic-settings（配置）

## 关键约束
- 币安 API 需要代理：proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
- 所有模块通过 SQLite 解耦，不直接互调
- 日志统一用 structlog JSON 格式
- 配置走 .env + pydantic-settings，密钥不入库
- 代码要有类型标注，函数有 docstring

## 目录结构
```
crypto-quant/
├── config/
│   ├── .env.example
│   └── settings.py
├── data/
│   ├── collectors/
│   │   ├── binance_market.py
│   │   ├── onchain.py
│   │   └── fear_greed.py
│   └── db/
│       ├── models.py
│       └── migrations/
├── signals/
├── execution/
├── main.py
├── pyproject.toml
└── README.md
```

## 当前任务
阶段 1.1：项目初始化 + SQLite schema
阶段 1.2：3 个数据采集器（binance_market / onchain / fear_greed）
阶段 1.3：APScheduler + main.py 入口
