"""持仓监控器 — 定时拉取持仓 + 自动止盈规则

止盈规则：
- 浮盈 >= 3x 止损额 → 移动止损到保本价
- 浮盈 >= 5x 止损额 → 移动止损锁定利润
- 浮盈 >= 10x 止损额 → 平半仓
- 持仓 > 48h → 强制平仓
"""

from datetime import datetime, timezone
from typing import Any

import ccxt
import structlog
from sqlalchemy import desc
from sqlalchemy.orm import Session as DBSession

from config.settings import settings
from data.db.models import Trade, get_session
from execution.portfolio import get_account_balance, get_stop_loss_amount, get_trading_exchange

log = structlog.get_logger()


# 止盈倍数常量
TP_BREAKEVEN_MULTIPLIER = 3      # 3x → 移止损到保本
TP_LOCK_PROFIT_MULTIPLIER = 5    # 5x → 移止损锁定利润
TP_HALF_CLOSE_MULTIPLIER = 10    # 10x → 平半仓
FORCE_CLOSE_HOURS = 48           # 48h → 强制平仓


def poll_positions(
    exchange: ccxt.binanceusdm | None = None,
    session: DBSession | None = None,
) -> list[dict[str, Any]]:
    """拉取当前持仓，执行止盈规则检查并返回操作结果。

    Args:
        exchange: ccxt 交易所实例（带 API 密钥）
        session: 数据库 session

    Returns:
        操作记录列表，每条包含 {symbol, action, detail}
    """
    own_session = False
    if session is None:
        session = get_session(settings.database_url)
        own_session = True

    if exchange is None:
        exchange = get_trading_exchange()

    actions: list[dict[str, Any]] = []

    try:
        positions = exchange.fetch_positions()
        now = datetime.now(timezone.utc)

        for pos in positions:
            symbol = str(pos.get("symbol", ""))
            size = float(pos.get("contracts", 0) or 0)
            side = str(pos.get("side", ""))
            entry_price = float(pos.get("entryPrice", 0) or 0)
            mark_price = float(pos.get("markPrice", 0) or 0)
            unrealized_pnl = float(pos.get("unrealizedPnl", 0) or 0)
            leverage = int(float(pos.get("leverage", 1) or 1))

            # 只处理做多仓位
            if size <= 0 or side.upper() != "LONG":
                continue

            # 查询 DB 的交易记录
            trade = _get_trade_record(session, symbol)

            # 计算止盈状态
            action = _evaluate_tp_rules(
                entry_price=entry_price,
                mark_price=mark_price,
                unrealized_pnl=unrealized_pnl,
                position_age_hours=_get_age_hours(trade, now),
                symbol=symbol,
                exchange=exchange,
            )

            if action:
                actions.append(action)
                log.info("tp_action_triggered", symbol=symbol, action=action["action"])

        log.info("positions_polled", count=len(actions))

    except Exception as e:
        log.error("poll_positions_failed", error=str(e))
    finally:
        if own_session:
            session.close()

    return actions


# ============================================================
# 止盈规则引擎
# ============================================================


def _evaluate_tp_rules(
    entry_price: float,
    mark_price: float,
    unrealized_pnl: float,
    position_age_hours: float,
    symbol: str,
    exchange: ccxt.binanceusdm,
) -> dict[str, Any] | None:
    """对单个仓位执行止盈规则链判断。

    Returns:
        action dict 或 None（无需操作）
        action = {
            "symbol": str,
            "action": "breakeven" | "lock_profit" | "half_close" | "force_close",
            "detail": str,
        }
    """
    if entry_price <= 0 or mark_price <= 0:
        return None

    # 1. 48h 强制平仓（最高优先级）
    if position_age_hours >= FORCE_CLOSE_HOURS:
        try:
            _force_close_position(exchange, symbol)
            return {
                "symbol": symbol,
                "action": "force_close",
                "detail": f"持仓 {position_age_hours:.1f}h >= {FORCE_CLOSE_HOURS}h，强制平仓",
            }
        except Exception as e:
            log.error("force_close_failed", symbol=symbol, error=str(e))
            return {
                "symbol": symbol,
                "action": "force_close_error",
                "detail": f"强制平仓失败: {e}",
            }

    # 计算浮盈相对于止损额的倍数
    equity = get_account_balance(exchange)
    risk_amount = get_stop_loss_amount(equity)
    if risk_amount <= 0:
        return None

    profit_multiple = unrealized_pnl / risk_amount

    # 2. 10x 平半仓
    if profit_multiple >= TP_HALF_CLOSE_MULTIPLIER:
        try:
            _half_close_position(exchange, symbol)
            return {
                "symbol": symbol,
                "action": "half_close",
                "detail": f"浮盈 {unrealized_pnl:.2f}u = {profit_multiple:.1f}x 止损额，平半仓",
            }
        except Exception as e:
            log.error("half_close_failed", symbol=symbol, error=str(e))
            return {
                "symbol": symbol,
                "action": "half_close_error",
                "detail": f"平半仓失败: {e}",
            }

    # 3. 5x 移止损锁定利润
    if profit_multiple >= TP_LOCK_PROFIT_MULTIPLIER:
        try:
            lock_price = entry_price * 1.02  # 锁定 2% 利润
            _update_stop_loss(exchange, symbol, lock_price)
            return {
                "symbol": symbol,
                "action": "lock_profit",
                "detail": f"浮盈 {unrealized_pnl:.2f}u = {profit_multiple:.1f}x，止损移至 {lock_price:.4f}",
            }
        except Exception as e:
            log.error("lock_profit_failed", symbol=symbol, error=str(e))
            return {
                "symbol": symbol,
                "action": "lock_profit_error",
                "detail": f"移止损失败: {e}",
            }

    # 4. 3x 移止损到保本
    if profit_multiple >= TP_BREAKEVEN_MULTIPLIER:
        try:
            _update_stop_loss(exchange, symbol, entry_price)
            return {
                "symbol": symbol,
                "action": "breakeven",
                "detail": f"浮盈 {unrealized_pnl:.2f}u = {profit_multiple:.1f}x，止损移至保本 {entry_price:.4f}",
            }
        except Exception as e:
            log.error("breakeven_move_failed", symbol=symbol, error=str(e))
            return {
                "symbol": symbol,
                "action": "breakeven_error",
                "detail": f"保本移止损失败: {e}",
            }

    return None


# ============================================================
# 交易执行辅助
# ============================================================


def _force_close_position(exchange: ccxt.binanceusdm, symbol: str) -> dict:
    """强制平仓 — 市价卖出全部持仓。"""
    try:
        exchange.load_markets()
        market = exchange.market(symbol)

        # 获取当前持仓量
        positions = exchange.fetch_positions([symbol])
        qty = 0.0
        for pos in positions:
            if pos.get("symbol") == symbol:
                qty = abs(float(pos.get("contracts", 0) or 0))
                break

        if qty <= 0:
            log.warning("no_position_to_close", symbol=symbol)
            return {"symbol": symbol, "action": "no_position"}

        order = exchange.create_market_sell_order(symbol, qty)
        log.info("force_close_executed", symbol=symbol, qty=qty, order_id=order.get("id"))
        return {"symbol": symbol, "action": "force_closed", "qty": qty}
    except Exception as e:
        log.error("force_close_failed", symbol=symbol, error=str(e))
        raise


def _half_close_position(exchange: ccxt.binanceusdm, symbol: str) -> dict:
    """平半仓 — 市价卖出 50% 持仓。"""
    try:
        exchange.load_markets()
        positions = exchange.fetch_positions([symbol])
        qty = 0.0
        for pos in positions:
            if pos.get("symbol") == symbol:
                qty = abs(float(pos.get("contracts", 0) or 0))
                break

        if qty <= 0:
            log.warning("no_position_to_half_close", symbol=symbol)
            return {"symbol": symbol, "action": "no_position"}

        half_qty = _round_down(qty / 2)
        if half_qty <= 0:
            log.warning("half_qty_too_small", symbol=symbol, qty=qty)
            # 数量太小无法平半，直接全平
            half_qty = qty

        order = exchange.create_market_sell_order(symbol, half_qty)
        log.info("half_close_executed", symbol=symbol, qty=half_qty, order_id=order.get("id"))
        return {"symbol": symbol, "action": "half_closed", "qty": half_qty}
    except Exception as e:
        log.error("half_close_failed", symbol=symbol, error=str(e))
        raise


def _update_stop_loss(exchange: ccxt.binanceusdm, symbol: str, stop_price: float) -> dict:
    """更新止���单（先取消旧止损，再下新止损单）。"""
    try:
        exchange.load_markets()

        # 获取当前持仓量
        positions = exchange.fetch_positions([symbol])
        qty = 0.0
        for pos in positions:
            if pos.get("symbol") == symbol:
                qty = abs(float(pos.get("contracts", 0) or 0))
                break

        if qty <= 0:
            return {"symbol": symbol, "action": "no_position"}

        # 取消当前挂着的止损单
        _cancel_stop_orders(exchange, symbol)

        # 下新的止损限价单
        # STOP_LOSS_LIMIT: 触发后以限价卖出
        order = exchange.create_order(
            symbol,
            type="STOP_LOSS_LIMIT",
            side="sell",
            amount=qty,
            price=stop_price * 0.99,  # 限价略低于触发价，确保成交
            params={
                "stopPrice": stop_price,
                "reduceOnly": True,
            },
        )
        log.info("stop_loss_updated", symbol=symbol, stop_price=stop_price)
        return {"symbol": symbol, "action": "stop_updated", "stop_price": stop_price}
    except Exception as e:
        log.error("update_stop_loss_failed", symbol=symbol, error=str(e))
        raise


def _cancel_stop_orders(exchange: ccxt.binanceusdm, symbol: str) -> None:
    """取消某交易对的所有当前止损单。"""
    try:
        open_orders = exchange.fetch_open_orders(symbol)
        for order in open_orders:
            order_type = str(order.get("type", ""))
            if "stop" in order_type.lower():
                exchange.cancel_order(order["id"], symbol)
                log.info("stop_order_cancelled", symbol=symbol, order_id=order["id"])
    except Exception as e:
        log.warning("cancel_stop_orders_failed", symbol=symbol, error=str(e))


# ============================================================
# DB 辅助
# ============================================================


def _get_trade_record(session: DBSession, symbol: str) -> Trade | None:
    """从 trades 表获取某标的最近未平仓交易记录。"""
    try:
        return (
            session.query(Trade)
            .filter(
                Trade.symbol == symbol,
                Trade.closed_at.is_(None),
            )
            .order_by(desc(Trade.opened_at))
            .first()
        )
    except Exception:
        return None


def _get_age_hours(trade: Trade | None, now: datetime) -> float:
    """计算持仓时长（小时）。"""
    if trade is None or trade.opened_at is None:
        return 0.0
    delta = now - trade.opened_at
    return delta.total_seconds() / 3600


def _round_down(value: float, precision: int = 6) -> float:
    """向下取整到指定精度。"""
    factor = 10**precision
    return int(value * factor) / factor
