"""持仓监控器 — 定时拉取持仓 + 止盈 + trailing stop

P1-⑥: 止盈阶梯改为 1.5x平半+保本 → 3x平剩75% → 5x清仓 → 48h兜底
     新增 trailing stop: 1.5x后启动，峰值回撤40%平仓
     peak_pnl 存入 trades.peak_pnl
"""

from datetime import datetime, timezone
from typing import Any

import ccxt
import structlog
from sqlalchemy import desc
from sqlalchemy.orm import Session as DBSession

from config.settings import settings
from data.db.models import Trade, get_session
from execution.portfolio import get_account_balance, get_trading_exchange

log = structlog.get_logger()


# P1-⑥: 新止盈阶梯
TP_HALF_AND_BREAKEVEN = 1.5   # 1.5x 平半 + 保本止损
TP_SELL_75PCT = 3.0           # 3x 平剩余 75%
TP_CLEAR = 5.0                # 5x 清仓
FORCE_CLOSE_HOURS = 48
TRAILING_DRAWDOWN = 0.4       # 从峰值回撤 40% 触发 trailing stop


def poll_positions(
    exchange: ccxt.binanceusdm | None = None,
    session: DBSession | None = None,
) -> list[dict[str, Any]]:
    """拉取当前持仓，执行止盈规则 + trailing stop 检查。"""
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

            if size <= 0 or side.upper() != "LONG":
                continue

            # 查 DB 交易记录
            trade = _get_trade_record(session, symbol)

            # 更新 peak_pnl
            if trade and unrealized_pnl > (trade.peak_pnl or 0):
                trade.peak_pnl = unrealized_pnl
                session.commit()

            action = _evaluate_tp_rules(
                entry_price=entry_price,
                mark_price=mark_price,
                unrealized_pnl=unrealized_pnl,
                position_age_hours=_get_age_hours(trade, now),
                symbol=symbol,
                exchange=exchange,
                trade=trade,
                session=session,
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
# 止盈规则引擎 (P1-⑥ 改版)
# ============================================================


def _evaluate_tp_rules(
    entry_price: float,
    mark_price: float,
    unrealized_pnl: float,
    position_age_hours: float,
    symbol: str,
    exchange: ccxt.binanceusdm,
    trade: Trade | None = None,
    session: DBSession | None = None,
) -> dict[str, Any] | None:
    if entry_price <= 0 or mark_price <= 0:
        return None

    # 计算盈亏比（相对于开仓时 risk_amount）
    risk_amount = trade.risk_amount if trade and trade.risk_amount > 0 else (
        get_account_balance(exchange) * settings.risk_per_trade)
    profit_multiple = unrealized_pnl / risk_amount if risk_amount > 0 else 0

    peak_pnl = trade.peak_pnl if trade else 0

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
            return {"symbol": symbol, "action": "force_close_error", "detail": f"强制平仓失败: {e}"}

    # 2. P1-⑥: 5x 清仓
    if profit_multiple >= TP_CLEAR:
        try:
            _force_close_position(exchange, symbol)
            return {
                "symbol": symbol,
                "action": "take_profit_5x",
                "detail": f"浮盈 {unrealized_pnl:.2f}u = {profit_multiple:.1f}x，清仓",
            }
        except Exception as e:
            return {"symbol": symbol, "action": "clear_error", "detail": f"清仓失败: {e}"}

    # 3. P1-⑥: 3x 平剩余 75%（去重：检查 closed_3x）
    if profit_multiple >= TP_SELL_75PCT and not (trade and trade.closed_3x):
        try:
            _partial_close(exchange, symbol, ratio=0.75)
            if trade and session:
                trade.closed_3x = True
                session.commit()
            _update_stop_loss(exchange, symbol, entry_price * 1.001)  # 保本
            return {
                "symbol": symbol,
                "action": "sell_75pct",
                "detail": f"浮盈 {unrealized_pnl:.2f}u = {profit_multiple:.1f}x，平剩余75%+保本",
            }
        except Exception as e:
            return {"symbol": symbol, "action": "sell_75pct_error", "detail": f"平75%失败: {e}"}

    # 4. P1-⑥: 1.5x 平半 + 保本止损（去重：检查 half_closed）
    if profit_multiple >= TP_HALF_AND_BREAKEVEN and not (trade and trade.half_closed):
        try:
            _partial_close(exchange, symbol, ratio=0.5)
            if trade and session:
                trade.half_closed = True
                session.commit()
            _update_stop_loss(exchange, symbol, entry_price)  # 保本
            return {
                "symbol": symbol,
                "action": "half_close_breakeven",
                "detail": f"浮盈 {unrealized_pnl:.2f}u = {profit_multiple:.1f}x，平半+保本",
            }
        except Exception as e:
            return {"symbol": symbol, "action": "half_close_error", "detail": f"平半失败: {e}"}

    # 5. P1-⑥: Trailing stop — 1.5x 后启动，峰值回撤 40% 平仓
    if profit_multiple >= TP_HALF_AND_BREAKEVEN and peak_pnl > 0:
        drawdown_from_peak = (peak_pnl - unrealized_pnl) / peak_pnl if peak_pnl > 0 else 0
        if drawdown_from_peak >= TRAILING_DRAWDOWN:
            try:
                _force_close_position(exchange, symbol)
                return {
                    "symbol": symbol,
                    "action": "trailing_stop",
                    "detail": f"峰值 {peak_pnl:.2f}u 回撤 {drawdown_from_peak*100:.0f}% >= {TRAILING_DRAWDOWN*100:.0f}%，trailing平仓",
                }
            except Exception as e:
                return {"symbol": symbol, "action": "trailing_error", "detail": f"trailing失败: {e}"}

    return None


# ============================================================
# 交易执行辅助
# ============================================================


def _force_close_position(exchange: ccxt.binanceusdm, symbol: str) -> dict:
    try:
        exchange.load_markets()
        positions = exchange.fetch_positions([symbol])
        qty = 0.0
        for pos in positions:
            if pos.get("symbol") == symbol:
                qty = abs(float(pos.get("contracts", 0) or 0))
                break
        if qty <= 0:
            return {"symbol": symbol, "action": "no_position"}
        order = exchange.create_market_sell_order(symbol, qty)
        log.info("force_close_executed", symbol=symbol, qty=qty, order_id=order.get("id"))
        return {"symbol": symbol, "action": "force_closed", "qty": qty}
    except Exception as e:
        log.error("force_close_failed", symbol=symbol, error=str(e))
        raise


def _partial_close(exchange: ccxt.binanceusdm, symbol: str, ratio: float) -> dict:
    """部分平仓（ratio: 0.5 = 平半, 0.75 = 平 75%）。"""
    try:
        exchange.load_markets()
        positions = exchange.fetch_positions([symbol])
        qty = 0.0
        for pos in positions:
            if pos.get("symbol") == symbol:
                qty = abs(float(pos.get("contracts", 0) or 0))
                break
        if qty <= 0:
            return {"symbol": symbol, "action": "no_position"}
        close_qty = _round_down(qty * ratio)
        if close_qty <= 0:
            close_qty = qty
        order = exchange.create_market_sell_order(symbol, close_qty)
        log.info("partial_close_executed", symbol=symbol, qty=close_qty, ratio=ratio)
        return {"symbol": symbol, "action": "partial_closed", "qty": close_qty}
    except Exception as e:
        log.error("partial_close_failed", symbol=symbol, error=str(e))
        raise


def _update_stop_loss(exchange: ccxt.binanceusdm, symbol: str, stop_price: float) -> dict:
    try:
        exchange.load_markets()
        positions = exchange.fetch_positions([symbol])
        qty = 0.0
        for pos in positions:
            if pos.get("symbol") == symbol:
                qty = abs(float(pos.get("contracts", 0) or 0))
                break
        if qty <= 0:
            return {"symbol": symbol, "action": "no_position"}
        _cancel_stop_orders(exchange, symbol)
        order = exchange.create_order(
            symbol, type="STOP_LOSS_LIMIT", side="sell",
            amount=qty, price=stop_price * 0.99,
            params={"stopPrice": stop_price, "reduceOnly": True},
        )
        log.info("stop_loss_updated", symbol=symbol, stop_price=stop_price)
        return {"symbol": symbol, "action": "stop_updated", "stop_price": stop_price}
    except Exception as e:
        log.error("update_stop_loss_failed", symbol=symbol, error=str(e))
        raise


def _cancel_stop_orders(exchange: ccxt.binanceusdm, symbol: str) -> None:
    try:
        for order in exchange.fetch_open_orders(symbol):
            if "stop" in str(order.get("type", "")).lower():
                exchange.cancel_order(order["id"], symbol)
                log.info("stop_order_cancelled", symbol=symbol, order_id=order["id"])
    except Exception as e:
        log.warning("cancel_stop_orders_failed", symbol=symbol, error=str(e))


# ============================================================
# DB 辅助
# ============================================================


def _get_trade_record(session: DBSession, symbol: str) -> Trade | None:
    try:
        return (
            session.query(Trade)
            .filter(Trade.symbol == symbol, Trade.closed_at.is_(None))
            .order_by(desc(Trade.opened_at))
            .first()
        )
    except Exception:
        return None


def _get_age_hours(trade: Trade | None, now: datetime) -> float:
    if trade is None or trade.opened_at is None:
        return 0.0
    return (now - trade.opened_at).total_seconds() / 3600


def _round_down(value: float, precision: int = 6) -> float:
    factor = 10 ** precision
    return int(value * factor) / factor
