# 交易总开关 / dry-run / watchdog 加固计划

> 日期：2026-04-30
> 背景：`main.py` 曾因 `calculate_position_size()` 数量精度死循环导致 CPU 100%。当前主服务已停止，死循环根因已修复并 push。
> 目标：在不急着恢复自动交易的前提下，给系统加三层安全保险：交易总开关、dry-run、watchdog。

---

## 0. 设计原则

1. **先防误交易，再防卡死，最后做仿真。**
2. 当前阶段优先保证：系统能采集、打分、监控，但不会意外开新仓。
3. 所有开关必须走 `config/settings.py` + `.env`，不要硬编码。
4. 交易执行层必须自己检查开关，不能只靠 `main.py` 外层判断。
5. 每个开关都要有明确日志，方便 Notion/Hermes 判断当前模式。

---

## 1. 交易总开关（优先级最高，建议立即加）

### 作用

像“总电闸”：

- 行情采集继续跑
- 信号打分继续跑
- 持仓监控继续跑
- **禁止任何新开仓**

适合当前阶段：demo 调试、排查 bug、观察信号，但不想再新增仓位。

### 配置项

文件：`config/settings.py`

新增：

```python
trading_enabled: bool = False
```

文件：`config/.env`

新增：

```env
TRADING_ENABLED=false
```

默认建议：`False`。

### 接入位置

#### A. `main.py::run_trading_cycle`

在“获取 Top 信号 / 逐个开仓”之前检查：

```python
if not settings.trading_enabled:
    log.info("trading_disabled_skip_new_entries")
    return
```

注意：这个检查必须放在 `poll_positions()` 之后。

原因：即使禁止新开仓，也要继续检查已有仓位止损/止盈。

#### B. `execution/order_manager.py::place_market_long`

函数内部也要检查：

```python
if not settings.trading_enabled:
    log.warning("place_order_blocked_trading_disabled", symbol=symbol)
    return None
```

原因：防止其他入口绕过 `main.py` 直接调用 `place_market_long()`。

### 验证

1. 设置 `TRADING_ENABLED=false`
2. 手动跑一次 `run_trading_cycle()`
3. 预期：
   - `poll_positions` 正常执行
   - 日志出现 `trading_disabled_skip_new_entries`
   - 不出现 `placing_market_long`
   - 不调用 `create_market_buy_order`

---

## 2. watchdog（第二优先级，建议交易总开关之后马上加）

### 作用

像“看门狗”：监控单次交易循环是否卡死。

这次 CPU 100% 的问题，虽然根因已修，但 watchdog 可以防止未来类似 bug 把进程跑满几十分钟。

### 第一版 watchdog 范围

先做轻量版，不上复杂守护进程：

- 单次 `run_trading_cycle()` 超过 N 秒：记录 error + TG 告警
- APScheduler 禁止交易任务重叠：`max_instances=1`, `coalesce=True`
- 每个关键阶段记录耗时日志

### 配置项

文件：`config/settings.py`

新增：

```python
trading_cycle_timeout_seconds: int = 120
watchdog_enabled: bool = True
```

`.env`：

```env
WATCHDOG_ENABLED=true
TRADING_CYCLE_TIMEOUT_SECONDS=120
```

### 接入位置

#### A. `main.py` scheduler 注册

交易循环 job 增加：

```python
scheduler.add_job(
    run_trading_cycle,
    "interval",
    seconds=settings.trading_cycle_interval,
    id="trading_cycle",
    name="交易循环",
    max_instances=1,
    coalesce=True,
    misfire_grace_time=60,
)
```

说明：

- `max_instances=1`：上一轮没结束，下一轮不再启动
- `coalesce=True`：错过多轮只补一次，不堆积
- `misfire_grace_time=60`：延迟太久就跳过

#### B. `main.py::run_trading_cycle`

用 `time.monotonic()` 记录总耗时和阶段耗时：

```python
started = time.monotonic()
...
elapsed = time.monotonic() - started
if elapsed > settings.trading_cycle_timeout_seconds:
    log.error("trading_cycle_slow", elapsed=elapsed)
    notify_system_alert(...)
```

第一版不做强制 kill，因为 Python 线程内无法安全杀掉自己。

### 第二版 watchdog（后续可做）

如果第一版还不够，再做外部 watchdog：

- 独立脚本 `scripts/watchdog.py`
- 检查 `main.py` CPU、最近日志时间、交易循环是否超时
- 发现异常：发 TG + 停止进程

但这个要谨慎，因为它涉及自动杀进程。

### 验证

1. 设置 timeout 比较小，比如 1 秒
2. 人为 sleep 2 秒模拟慢任务
3. 确认日志有 `trading_cycle_slow`
4. 确认 TG 告警可发
5. 恢复 timeout 120 秒

---

## 3. dry-run（稍后加，不建议现在优先）

### 作用

像“模拟下单”：

- 完整跑到下单前
- 计算出订单参数、止损价、仓位大小
- **不调用交易所真实下单 API**
- 只记录日志 / Notion / TG

### 什么时候适合加

建议时机：

1. 当前 demo 进程稳定运行至少 24-48 小时
2. 没有新的 CPU 100%、死循环、DB 错配问题
3. 准备从 demo trading 切到实盘前
4. 想观察“如果是真交易，系统会下哪些单”

也就是说：

- **现在：先加交易总开关 + watchdog**
- **demo 稳定后：加 dry-run**
- **切实盘前：dry-run 至少跑 1-3 天**

### 配置项

文件：`config/settings.py`

新增：

```python
dry_run: bool = True
```

`.env`：

```env
DRY_RUN=true
```

### 接入位置

文件：`execution/order_manager.py::place_market_long`

在完成：

- 合约验证
- 风控检查
- 最大持仓检查
- 当前价格获取
- 仓位计算

之后，在真正调用：

```python
exchange.create_market_buy_order(...)
```

之前判断：

```python
if settings.dry_run:
    log.info("dry_run_order", symbol=symbol, qty=size_result.quantity, ...)
    return {..., "dry_run": True}
```

### 注意点

`dry_run` 和 `trading_enabled` 关系：

- `trading_enabled=false`：连模拟下单也不走，直接不交易
- `trading_enabled=true + dry_run=true`：走完整交易决策，但不真实下单
- `trading_enabled=true + dry_run=false`：真实下单

推荐切换路径：

```text
当前：trading_enabled=false, dry_run=true/false 都无所谓
观察期：trading_enabled=true, dry_run=true
实盘：trading_enabled=true, dry_run=false
```

### 验证

1. `TRADING_ENABLED=true` + `DRY_RUN=true`
2. 用高分信号触发开仓流程
3. 预期：
   - 出现 `dry_run_order`
   - 不出现真实 order_id
   - 交易所无新增订单
   - DB 不写入真实 open trade，或写入 `dry_run=True` 的单独记录（建议后续设计）

---

## 4. 推荐执行顺序

### 第一步：交易总开关（立即）

改动小，收益最大。

文件：

- `config/settings.py`
- `config/.env`
- `main.py`
- `execution/order_manager.py`
- `CLAUDE.md`

验证通过后 commit。

### 第二步：watchdog 轻量版（立即）

文件：

- `config/settings.py`
- `main.py`
- `notifications/tg.py`（如果已有系统告警函数就复用）
- `CLAUDE.md`

验证通过后 commit。

### 第三步：dry-run（demo 稳定 24-48 小时后）

文件：

- `config/settings.py`
- `execution/order_manager.py`
- 可选：DB schema 增加 dry_run 标记（不建议现在动，后续统一迁移）
- `CLAUDE.md`

---

## 5. 风险与取舍

### 交易总开关风险

低。唯一风险是误以为系统还会新开仓，但它不会。

解决：启动日志打印当前模式：

```text
trading_mode: enabled=false, dry_run=true/false
```

### watchdog 风险

中低。第一版只告警不杀进程，风险可控。

不要一上来做自动 kill，避免误杀正常长任务。

### dry-run 风险

中。因为它会改变 `place_market_long()` 的返回语义，要仔细设计 DB 是否记录模拟单。

建议后做。

---

## 6. 当前建议

现在先做：

1. `TRADING_ENABLED=false` 交易总开关
2. `WATCHDOG_ENABLED=true` + 交易循环耗时日志 + APScheduler 防重叠

暂不重启真实主服务，先手动跑一轮 `run_trading_cycle()` 验证行为。

验证通过后，再决定是否后台启动。
