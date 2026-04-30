# 修复计划：新模块审查发现的问题（Plan Mode）

## 目标
修复 GitNexus 重建索引后，在新模块审查中发现的 2 个有效问题，确保阶段 3 自动化链路稳定：
- `scripts/verify_stage3.py`
- `scripts/health_check.py`

## 当前上下文
- 索引已重建：`1101 nodes / 1892 edges / 60 flows`
- 新模块已上线并接入调度：
  - `verify_stage3.py`
  - `health_check.py`
  - `push_dashboards.py`
- 当前已识别问题：
  1. `check_risk_pause` 连续止损阈值硬编码（`>=3`）
  2. `health_check` 内部 `push_system_status()` 没有超时保护，可能阻塞调度线程

## 修复策略
只做“最小必要改动”：
- 不改业务策略
- 不改数据库结构
- 只修稳定性与配置一致性

---

## 执行步骤

### 步骤 1：修复 `verify_stage3.py` 的硬编码阈值
**目标**：风控判断与配置一致，避免后续调参失效。  
**改动**：
- 文件：`scripts/verify_stage3.py`
- 位置：`check_risk_pause()`
- 变更：`state.consecutive_stops >= 3` → `state.consecutive_stops >= settings.max_consecutive_stops`

**验证**：
- `pytest tests/test_verify_stage3.py -q`
- 手动运行：`PYTHONPATH=. python scripts/verify_stage3.py`

---

### 步骤 2：为 `health_check.py` 的 Notion 推送增加超时保护
**目标**：避免 Notion API 慢响应拖垮健康检查调度。  
**改动**：
- 文件：`scripts/health_check.py`
- 位置：`run_health_check()` 内 `push_system_status()` 调用块
- 方案：
  - 用后台线程执行 `push_system_status()`
  - 主流程 `join(timeout=15)`
  - 超时仅 `log.warning("dashboard_push_timeout")`，不阻塞健康检查主逻辑

**验证**：
- `PYTHONPATH=. python scripts/health_check.py`
- 观察日志：健康检查能按时完成，即便 Notion 偶发慢响应

---

### 步骤 3：回归验证（模块级 + 全量）
**目标**：确认修复不破坏现有功能。

**执行**：
1. `pytest tests/test_verify_stage3.py -q`
2. `pytest tests/ -q`
3. 手动 smoke：
   - `PYTHONPATH=. python scripts/verify_stage3.py`
   - `PYTHONPATH=. python scripts/health_check.py`

**通过标准**：
- 单测全绿
- 脚本可执行
- 无新异常堆栈

---

### 步骤 4：交付与记录
**提交内容**：
- 两处 bug 修复 + 必要日志
- 不引入额外行为变更

**记录更新**：
- Notion 留言板追加“修复完成 + 验证结果”
- 如有必要更新本地审计文档

## 预计改动文件
- `scripts/verify_stage3.py`
- `scripts/health_check.py`

## 风险与缓解
- 风险：线程超时保护可能掩盖 Notion 持续故障
- 缓解：保留 `dashboard_push_timeout` 日志，后续可加计数阈值触发 TG 告警

## 开放项（可选后续）
1. 给 `health_check` 增加“连续 N 次 dashboard timeout 才告警”机制（降噪）
2. 给 `verify_stage3` 增加 `--json` 输出，方便 Claude Code 后续自动消费
3. 补 `tests/test_health_check.py`（当前尚未覆盖该脚本的超时分支）
