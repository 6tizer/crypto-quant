# Crypto Quant — 加密量化交易系统

## 项目概述
加密货币合约量化交易系统，三层分工架构：
- **程序层**（Python 24/7）：数据采集 → 信号打分 → 交易执行 → 风控 → 持仓监控
- **AI 层**（Hermes Skill）：市场环境判断、异常诊断、复盘分析（阶段 4）
- **人**：审批决策、资金管理

当前阶段：**阶段 3（交易执行 + 风控）已完成，进入实盘调试**

## 技术栈
- Python 3.12+, uv 包管理
- SQLite（开发）→ PostgreSQL（生产）
- SQLAlchemy ORM（数据模型在 `data/db/models.py`）
- APScheduler 定时调度（`main.py`）
- ccxt（币安 U 本位合约 API）
- httpx（异步 HTTP）/ requests（Notion API）
- structlog（JSON 日志，统一用 `structlog.get_logger()`）
- pydantic-settings（`config/settings.py`，从 `config/.env` 读取）

## 运行方式
```bash
uv sync                              # 安装依赖
PYTHONPATH=. uv run python scripts/verify.py   # 验证所有采集器
PYTHONPATH=. uv run python main.py             # 启动调度服务（常驻）
PYTHONPATH=. uv run python -m backtest.simple_backtest  # 回测
```

## 目录结构
```
crypto-quant/
├── config/
│   ├── settings.py          # pydantic-settings，所有配置项定义
│   └── .env                 # 密钥 + 参数（不入 git）
├── data/
│   ├── collectors/          # 数据采集器
│   │   ├── binance_market.py   # 币安合约行情（5min）
│   │   ├── onchain.py          # DeFiLlama TVL（4h）
│   │   └── fear_greed.py       # 恐惧贪婪指数（每日）
│   ├── db/
│   │   └── models.py        # SQLAlchemy 表定义（MarketSnapshot, SignalScore, Trade, RiskState 等）
│   └── push/
│       └── notion_dashboard.py  # 推送到 Notion HUB（信号排行/每日市场/系统状态）
├── signals/
│   ├── momentum.py          # 涨幅异动信号
│   ├── scorer.py            # 综合打分引擎（策略 A 加权 + 策略 B 链上指标）
│   ├── strategy_switcher.py # A/B 策略切换
│   ├── whitelist.py         # 新币白名单过滤
│   └── bot_filter.py        # Bot 标的过滤
├── execution/
│   ├── order_manager.py     # 开仓（ccxt 市价多 + 止损联动）
│   ├── position_monitor.py  # 持仓监控 + 阶梯止盈（1.5x平半→3x平75%→5x清仓）+ trailing stop
│   ├── portfolio.py         # 仓位计算 + 账户余额
│   └── risk_guard.py        # 风控（连续止损/日亏/总回撤/恐贪暂停线）
├── backtest/
│   ├── data_downloader.py   # 历史数据下载
│   └── simple_backtest.py   # 简易回测
├── notifications/
│   └── tg.py                # Telegram 通知（开仓/平仓/风控事件）
├── utils/
│   └── symbol.py            # 标的符号转换（DB ↔ 交易所格式）
├── main.py                  # 主入口，APScheduler 注册所有定时任务
└── pyproject.toml
```

## 关键约束（必读）
1. **代理必填**：币安 API 需走代理 `http://127.0.0.1:7897`，ccxt 构造时必须传 `proxies` 参数
2. **模块解耦**：所有模块通过 SQLite 读写通信，不直接互调（采集器写表 → 打分器读表 → 交易器读表）
3. **只做多不做空**：`order_manager.py` 只实现市价开多
4. **止损动态计算**：早期（净值 < 1000u）用百分比（20%），后期切固定金额（200u）
5. **止盈阶梯**：1.5x 平半 + 保本 → 3x 平 75% → 5x 清仓 → 48h 强制平仓；1.5x 后启动 trailing stop（峰值回撤 40%）
6. **安全开关**：`trading_enabled=False` 时只做持仓监控，不开新仓；`watchdog_enabled=True` 时记录慢交易循环
7. **风控四条线**：连续止损 3 次暂停 / 日亏上限 / 总回撤 30% 暂停 / 恐贪 < 15 暂停
8. **日志统一**：全部用 `structlog.get_logger()`，输出 JSON 格式，不要用 print 或标准 logging
9. **配置单一入口**：所有参数在 `config/settings.py` 定义，密钥走 `.env`，代码里不要硬编码
10. **代码规范**：类型标注 + docstring，SQLAlchemy 查询注意 session 关闭（try/finally）
11. **阶段 1-3 用 testnet**：`binance_demo_trading: true`，不需要真实 API Key

## Notion HUB
- 项目文档中心在 Notion（Crypto Quant HUB），含留言板数据库、Agent 接入指南
- `data/push/notion_dashboard.py` 定时推送 3 个 inline DB：信号排行 / 每日市场 / 系统状态
- Hermes 被视为"AI 运营官"角色，应遵循 HUB 中的 Agent 接入清单

## 信号体系
- **策略 A**（趋势型）：momentum 0.54 + oi_divergence 0.31 + whitelist 0.15
- **策略 B**（链上型，阶段 5 补全）：kronos + mvrv + sopr + etf + smart_money + fear_greed
- 入场阈值：`entry_threshold = 0.50`（v5 回测确认）
- 策略切换阈值：`strategy_switch_threshold = 1.5`

## 数据库表（核心）
- `market_snapshots`：行情快照（symbol, price, oi, volatility, funding_rate...）
- `signal_scores`：信号打分结果（各维度分数 + 加权总分 + 策略类型）
- `trades`：交易记录（开仓价、止损、止盈阶梯状态、peak_pnl）
- `risk_state`：风控状态（连续止损计数、日亏累计、回撤）
- `fear_greed_history`：恐贪指数历史

## 开发规范（强制）
来源：Notion 开发规范文档。所有新代码和修改必须遵守。

### 符号格式（§1 — Bug 最大根因）
- 内部统一用 `BASE/USDT`（如 `SKYAI/USDT`）
- 与交易所交互时用 `BASE/USDT:USDT`
- **禁止手动拼/删后缀**，所有转换必须走 `utils/symbol.py` 的 `to_exchange_symbol()` / `to_db_symbol()`
- 数据库存储用 DB 格式，查询交易所前转换，交易所返回后立即转回

### 类型安全（§2）
- 所有函数必须有完整 type hints（参数 + 返回值）
- 禁止用 `Any` 做参数类型，除非有充分理由并注释
- ccxt 返回值立即做类型断言，不把 `dict | None` 直接传递（必须检查 `None` 和 `<= 0`）
- 目标：mypy 零 error 才能合并

### 测试（§3）
- 框架：pytest + pytest-mock（mock ccxt exchange 调用）
- 目录：`tests/`，结构镜像 `execution/`、`signals/`
- L1 必须覆盖：开仓/平仓/止损执行、符号格式转换、止盈阶梯逻辑、风控暂停判断
- 每次新增功能必须同时提交测试

### Session 管理（§4）
- 调度入口（`main.py` 的 `run_xxx` 函数）负责创建和关闭 session
- 业务函数一律接受 `session` 参数，不自己创建
- 同一个交易循环内共用同一个 session，避免数据不一致
- 如果必须兼容无 session 调用，用 `own_session` 模式但在 `finally` 关闭

### 代码审查清单（§5）
**P0 资金安全级：**
- 止损单失败时必须立即市价平仓或标记 `stop_loss_missing=True` + TG 告警，不允许裸仓
- 所有价格/金额计算必须显式检查 `None` 和 `<= 0`
- 止损/止盈计算必须基于实际账户余额，不硬编码
- 入场前必须过风控检查 `check_risk_status()`
- 开仓后必须确认止损单成交，失败则立即平仓

**P1 逻辑正确性：**
- 非合约/已下线标的过滤必须在 `order_manager` 内部做，不只靠 `main.py`
- Demo 余额计算必须包含未实现盈亏
- `exchange` 实例在交易循环入口创建一次，传给所有子函数
- 符号格式在 DB ↔ 交易所边界必须转换

**P2 代码质量：**
- 有类型标注 + docstring
- 无 mypy error
- README 和文档与实际阶段同步

## 已知问题（待修复）
1. 🔵 整个仓库 0 个测试（待建，demo 跑稳后专项）
2. 🔵 mypy 扫描有 89 个 warning（大部分是 SQLAlchemy ORM Column 类型误报，待逐步消除）

## 已修复（2026-04-30 审计）
> 详见 `iCloud/Hermes/crypto-quant-audit-2026-04-30.md`

**Round 1 — commit `cfa0a31`（P0×3 + P1×5）：**
- P0-1: Demo/实盘止损模式自动切换（避免双重止损）
- P0-2: 止损单失败 TG 告警
- P0-3: Demo 余额计算加未平仓保证金扣除
- P1-1: 7处 `except Exception: pass` 全部加日志
- P1-2: 日亏损归零定时任务（cron UTC 00:00）
- P1-4: exchange 提前创建复用 + handle_take_profit 接入全仓平仓
- P1-5: `_get_price_precision` 步长→位数转换修复

**Round 2 — commit `e9a233c`（P2×3）：**
- P2-2: pyproject.toml 加 `[tool.mypy]` 配置
- P2-3: 止盈阶梯/交易间隔/max_positions 魔法数字提取到 settings
- P2-4: handle_take_profit 已合并到 P1-4B 修复

**Round 3 — commit `9aee43a`（生产阻塞 Bug）：**
- 修复 `calculate_position_size()` 数量精度死循环导致 `main.py` CPU 100%
- 根因：ccxt/binance `precision.amount` 在 TICK_SIZE 模式下返回步长（如 `0.01`），旧 `_round_to_precision` 错当小数位数 `int(0.01)=0`，导致 min_notional 补量循环里 quantity 永远为 0
- 修复：新增 `_precision_to_step()`，按步长 floor，并给补量 while 加 1000 次 guard

## 已知坑
- `utils/symbol.py` 处理币安合约标的格式差异，新增标的注意测试
- ccxt 的 `create_order` 需要 `exchange.load_markets()` 先加载市场信息
- ccxt/binance `precision.amount` 可能是步长（TICK_SIZE，如 `0.01` / `1.0`），不是小数位数；数量舍入必须用 step 逻辑，不要 `int(precision)`
- 数据库文件在 `data/crypto_quant.db`，相对路径，必须在项目根目录运行

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **crypto-quant** (913 symbols, 1524 relationships, 54 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/crypto-quant/context` | Codebase overview, check index freshness |
| `gitnexus://repo/crypto-quant/clusters` | All functional areas |
| `gitnexus://repo/crypto-quant/processes` | All execution flows |
| `gitnexus://repo/crypto-quant/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
