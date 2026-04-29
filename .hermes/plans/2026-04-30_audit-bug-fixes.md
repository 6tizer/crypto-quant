# 审计 Bug 全量修复计划

> 日期：2026-04-30
> 基准：commit d111c0b + 审计报告 iCloud/Hermes/crypto-quant-audit-2026-04-30.md
> 目标：修复审计发现的 13 个问题（P0×4 + P1×5 + P2×4）

---

## 修复清单

### 🔴 P0-1: 实盘/止损模式切换（条件单 vs 轮询）

**问题：** 实盘下条件单 + Python 轮询会双重止损
**文件：** `execution/position_monitor.py`
**方案：**
- `poll_positions` 止损阶段开头检查：如果 `settings.binance_demo_trading == False`，跳过 Python 轮询止损（信任交易所条件单）
- 只在 Demo 模式下走轮询止损
**验证：** 添加日志 `stop_check_mode: demo=轮询 / live=跳过`

### 🔴 P0-2: 止损单失败加 TG 告警

**问题：** 止损单失败只打 error 日志，无告警
**文件：** `execution/order_manager.py:168-170`
**方案：**
- 止损单失败时调用 `notify_stop_loss(symbol, risk_amount)` 发 TG 告警
- 在 trade 记录的 entry_reason 里标注 `止损单未挂`
**验证：** 模拟止损单失败场景，确认 TG 收到通知

### 🟡 P0-3: Demo 余额计算加持仓保证金

**问题：** `_get_balance_from_db()` 只算已平仓 PnL，忽略持仓保证金
**文件：** `execution/portfolio.py:76-89`
**方案：**
- 查 DB 所有未平仓 trade，计算 `sum(quantity × entry_price / leverage)` 作为已占保证金
- 返回 `initial_capital + total_pnl - used_margin`
**验证：** 对比当前持仓的实际保证金占用

### ✅ P0-4: 风控绕过 — 无需修改（已确认安全）

---

### 🟡 P1-1: except Exception 静默吞错（7 处）

**文件 + 修复方案：**

| # | 文件:行 | 当前行为 | 修复 |
|---|---------|----------|------|
| 1 | position_monitor.py:93-94 | TG 通知失败 `except: pass` | 改为 `except Exception as e: log.warning("tg_notify_failed", error=str(e))` |
| 2 | position_monitor.py:346-347 | `_get_trade_record` 失败返回 None | 改为 `except Exception as e: log.warning("get_trade_record_failed", symbol=symbol, error=str(e))` |
| 3 | position_monitor.py:359-360 | `_get_actual_position_qty` 失败返回 0.0 | 改为 `except Exception as e: log.error("get_actual_qty_failed", symbol=symbol, error=str(e))` |
| 4 | portfolio.py:86-87 | 余额计算失败返回 initial_capital | 改为 `log.error("balance_calc_failed", error=str(e))` 再 return |
| 5 | portfolio.py:207-208 | 精度获取失败返回 2 | 改为 `log.warning("price_precision_failed", symbol=symbol, error=str(e))` |
| 6 | risk_guard.py:264-265, 268-269 | `_get_equity` 失败返回 0.0 | 已安全（返回 0 → 触发暂停），加 `log.warning` |
| 7 | risk_guard.py:306-307 | 恐贪获取失败返回 None | 改为 `log.warning("fear_greed_fetch_failed", error=str(e))` |

### 🔴 P1-2: 日亏损归零定时任务（最紧急）

**问题：** `reset_daily_loss()` 存在但从未被调用，日亏只累加不归零
**文件：** `main.py`
**方案：** 在 `main()` 的 scheduler 注册区加：
```python
scheduler.add_job(
    reset_daily_loss,
    "cron",
    hour=0,
    minute=0,
    id="reset_daily_loss",
    name="日亏损归零",
)
```
**验证：** 检查 UTC 00:00 日亏损字段是否归零

### 🟡 P1-3: 定时任务执行顺序

**问题：** 采集、打分、交易三个任务并行启动
**方案：** 低优先级，暂不修。后续可改为链式调度。
**标记：** DEFERRED

### 🟡 P1-4: exchange 复用 + handle_take_profit 接入

**问题 A：** main.py 的 `run_trading_cycle` 没传 exchange 给 `check_risk_status`
**文件：** `main.py:118`
**方案：** 在 `run_trading_cycle` 开头创建 `exchange = get_trading_exchange()`，传给 `check_risk_status` 和 `poll_positions`

**问题 B：** `_partial_close` 和 `_force_close_position` 止盈平仓后没调用 `handle_take_profit` 更新 DB
**文件：** `execution/position_monitor.py`
**方案：** 在 `_evaluate_tp_rules` 中每个平仓成功后，调用 `handle_take_profit` 更新 DB
- 但注意：部分平仓（1.5x 平半、3x 平 75%）不应关闭 trade 记录，只更新 half_closed/closed_3x 标记
- 只有 `force_close`（5x 清仓、48h 兜底、trailing stop）才应调用 `handle_take_profit` 关闭 trade
- 需要区分「部分平仓」和「全仓平仓」

### 🔴 P1-5: _get_price_precision 精度不一致

**问题：** order_manager 的 `_get_price_precision` 返回原始步长（如 1e-05），没做步长→位数转换
**文件：** `execution/order_manager.py:378-384`
**方案：** 复用 portfolio.py 的转换逻辑（`-log10`），或统一提取到 `utils/precision.py`
```python
def _get_price_precision(exchange, symbol):
    try:
        raw = exchange.market(symbol).get("precision", {}).get("price", 2)
        if isinstance(raw, float) and raw < 1:
            import math
            return int(round(-math.log10(raw)))
        return int(raw)
    except Exception as e:
        log.warning("price_precision_failed", symbol=symbol, error=str(e))
        return 2
```
**验证：** 确认 SKYAI/USDT 的止损价精度正确

---

### 🔵 P2-1 ~ P2-4: 代码质量（不在本轮修）

- P2-1: 零测试 → 后续专项建 tests/
- P2-2: 无 mypy → 后续专项配置
- P2-3: 魔法数字 → 后续逐步提取到 settings
- P2-4: handle_take_profit 接入 → 已合并到 P1-4B

---

## 执行顺序（按优先级）

1. **P1-2** 日亏损归零定时任务 — 2 分钟，最紧急
2. **P1-5** 止损价精度修复 — 5 分钟
3. **P1-1** 7 处 except 静默吞错加日志 — 5 分钟
4. **P0-1** Demo/实盘止损模式切换 — 5 分钟
5. **P0-2** 止损单失败 TG 告警 — 3 分钟
6. **P0-3** Demo 余额计算加保证金 — 5 分钟
7. **P1-4A** exchange 复用 — 3 分钟
8. **P1-4B** handle_take_profit 接入 — 10 分钟

## 涉及文件

- `main.py` — 加定时任务 + exchange 复用
- `execution/order_manager.py` — 精度修复 + 止损告警
- `execution/position_monitor.py` — 模式切换 + 吞错修复 + take_profit 接入
- `execution/portfolio.py` — 余额计算 + 吞错修复
- `execution/risk_guard.py` — 吞错修复

## 验证

- 重启 main.py，等一轮交易循环确认无报错
- 检查日志：`positions_polled` 应有 `stop_check_mode` 字段
- 确认 `reset_daily_loss` 定时任务注册成功
- git commit + push
