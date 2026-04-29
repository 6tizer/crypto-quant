"""执行模块 — 交易执行、风控、持仓监控、仓位管理

公开接口：
- order_manager.place_market_long()   — 市价开多 + 止损联动
- risk_guard.check_risk_status()       — 综合风控检查
- risk_guard.record_stop_loss()        — 记录止损事件
- position_monitor.poll_positions()    — 定时拉持仓+止盈规则
- portfolio.calculate_position_size()  — 仓位/保证金计算
"""
