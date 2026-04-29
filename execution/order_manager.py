"""订单管理器 — ccxt 市价开多 + 止损限价联动

- 只做多不做空
- 开仓前过风控检查
- 止损金额动态计算（早期 20% 净值 / 后期固定 200u）
- 写入 trades 表
"""

from datetime import datetime, timezone
from typing import Any

import ccxt
import structlog
from sqlalchemy.orm import Session as DBSession

from config.settings import settings
from data.db.models import SignalScore, Trade, get_session
from execution.portfolio import (
    PositionSizeResult,
    calculate_position_size,
    can_open_new_position,
    get_account_balance,
    get_trading_exchange,
)
from execution.risk_guard import check_risk_status, record_stop_loss
from utils.symbol import to_db_symbol, to_exchange_symbol

log = structlog.get_logger()


def place_market_long(
    symbol: str,
    signal_score_id: int | None = None,
    strategy_type: str = "A",
    exchange: ccxt.binanceusdm | None = None,
    session: DBSession | None = None,
    atr: float = 0.0,  # P1-⑤: ATR for stop loss distance
) -> dict[str, Any] | None:
    """执行市价买入开多 + 联动止损限价单。

    Args:
        symbol: 交易对，如 "BTC/USDT:USDT"
        signal_score_id: 关联的信号打分记录 ID
        strategy_type: 策略类型 A/B
        exchange: ccxt 交易所实例（含 API 密钥）
        session: 数据库 session

    Returns:
        订单信息 dict（含关联的止损单信息），失败返回 None

    Raises:
        ValueError: 参数校验失败
        RuntimeError: 风控检查未通过
    """
    # ========== 1. 前置准备 ==========
    own_session = False
    if session is None:
        session = get_session(settings.database_url)
        own_session = True

    if exchange is None:
        exchange = get_trading_exchange()

    try:
        exchange.load_markets()

        # ========== 1.5 合约市场验证 ==========
        market = exchange.market(symbol) if symbol in exchange.markets else None
        if not market:
            ex_sym = to_exchange_symbol(symbol)
            market = exchange.market(ex_sym) if ex_sym in exchange.markets else None
        if not market:
            raise ValueError(f"{symbol} 不在交易所市场列表中")
        if not market.get("active", False):
            raise ValueError(f"{symbol} 已下线或不可交易")
        symbol = market["symbol"]  # 统一为交易所格式

        # ========== 2. 风控检查 ==========
        allowed, reason = check_risk_status(exchange, session)
        if not allowed:
            log.warning("order_blocked_by_risk", symbol=symbol, reason=reason)
            raise RuntimeError(f"风控未通过: {reason}")

        # ========== 3. 检查最大持仓数 ==========
        if not can_open_new_position(exchange, max_positions=settings.max_positions):
            raise RuntimeError("已达最大持仓数上限")

        # ========== 4. 获取当前价格 и 账户权益 ==========
        ticker = exchange.fetch_ticker(symbol)
        mark_price = float(ticker.get("last", 0) or 0)
        if mark_price <= 0:
            raise ValueError(f"无法获取 {symbol} 当前价格")

        equity = get_account_balance(exchange)
        if equity <= 0:
            raise ValueError("账户权益 <= 0，无法开仓")

        # ========== 5. 计算仓位 ==========
        size_result = calculate_position_size(
            exchange=exchange,
            symbol=symbol,
            equity=equity,
            entry_price=mark_price,
            strategy_type=strategy_type,
            atr=atr,  # P1-⑤
        )
        if size_result is None:
            log.warning("position_size_calc_failed", symbol=symbol)
            return None

        # ========== 6. 设置杠杆 ==========
        try:
            exchange.set_leverage(size_result.leverage, symbol)
        except Exception as e:
            log.warning("set_leverage_failed", symbol=symbol, leverage=size_result.leverage, error=str(e))

        # ========== 7. 执行市价开多 ==========
        log.info(
            "placing_market_long",
            symbol=symbol,
            qty=size_result.quantity,
            price=mark_price,
            leverage=size_result.leverage,
            risk_amount=size_result.risk_amount,
        )

        try:
            main_order = exchange.create_market_buy_order(symbol, size_result.quantity)
        except Exception as e:
            log.error("market_order_failed", symbol=symbol, error=str(e))
            raise RuntimeError(f"市价开多失败: {e}")

        order_id = str(main_order.get("id", ""))
        filled_qty = float(main_order.get("filled", size_result.quantity) or size_result.quantity)
        avg_price = float(main_order.get("price", mark_price) or mark_price)
        actual_cost = float(main_order.get("cost", 0) or 0)

        log.info("market_order_filled", symbol=symbol, order_id=order_id, qty=filled_qty, price=avg_price)

        # TG 通知
        from notifications.tg import notify_trade_open
        notify_trade_open(symbol, 0, avg_price, size_result.stop_loss_price)

        # ========== 8. 下止损限价单 ==========
        stop_order = None
        stop_order_id = None
        try:
            # STOP_LOSS_LIMIT: 触发价到达后以限价卖出
            stop_limit_price = round(size_result.stop_loss_price * 0.99, _get_price_precision(exchange, symbol))
            stop_order = exchange.create_order(
                symbol,
                type="STOP_LOSS_LIMIT",
                side="sell",
                amount=filled_qty,
                price=stop_limit_price,
                params={
                    "stopPrice": size_result.stop_loss_price,
                    "reduceOnly": True,
                },
            )
            stop_order_id = str(stop_order.get("id", ""))
            log.info(
                "stop_loss_order_placed",
                symbol=symbol,
                stop_price=size_result.stop_loss_price,
                stop_order_id=stop_order_id,
            )
        except Exception as e:
            log.error("stop_loss_order_failed", symbol=symbol, error=str(e))
            # 止损单失败发 TG 告警
            try:
                from notifications.tg import notify_stop_loss
                notify_stop_loss(symbol, size_result.risk_amount)
            except Exception:
                pass

        # ========== 9. 写入 trades 表 ==========
        trade = Trade(
            symbol=to_db_symbol(symbol),
            side="LONG",
            entry_price=avg_price,
            stop_loss_price=size_result.stop_loss_price,
            quantity=filled_qty,
            pnl=0,
            pnl_pct=0,
            strategy=strategy_type,
            signal_score_id=signal_score_id or 0,
            risk_amount=size_result.risk_amount,
            entry_reason=f"市价开多: 信号ID={signal_score_id}, 杠杆={size_result.leverage}x",
            opened_at=datetime.now(timezone.utc),
        )
        session.add(trade)
        session.commit()
        log.info("trade_record_saved", symbol=symbol, trade_id=trade.id)

        # ========== 10. 返回结果 ==========
        return {
            "symbol": symbol,
            "side": "LONG",
            "order_id": order_id,
            "stop_order_id": stop_order_id,
            "entry_price": avg_price,
            "quantity": filled_qty,
            "leverage": size_result.leverage,
            "margin": size_result.margin,
            "position_value": size_result.position_value,
            "stop_loss_price": size_result.stop_loss_price,
            "risk_amount": size_result.risk_amount,
            "strategy": strategy_type,
            "trade_id": trade.id,
        }

    except (ValueError, RuntimeError) as e:
        log.warning("place_order_rejected", symbol=symbol, error=str(e))
        raise
    except Exception as e:
        log.error("place_order_error", symbol=symbol, error=str(e))
        raise
    finally:
        if own_session:
            session.close()


def handle_stop_loss_triggered(
    symbol: str,
    exit_price: float,
    pnl: float,
    trade_id: int | None = None,
    exchange: ccxt.binanceusdm | None = None,
    session: DBSession | None = None,
) -> None:
    """处理止损触发后的善后：更新 trades 表 + 通知风控。

    Args:
        symbol: 交易对
        exit_price: 出场价格
        pnl: 盈亏金额 (USDT)
        trade_id: 可选的 trade_id，自动查找最新未平仓记录
        exchange: 交易所实例
        session: 数据库 session
    """
    own_session = False
    if session is None:
        session = get_session(settings.database_url)
        own_session = True

    try:
        # 查找并更新 trade 记录
        trade = None
        if trade_id:
            trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if trade is None:
            from sqlalchemy import desc as _desc
            trade = (
                session.query(Trade)
                .filter(Trade.symbol == to_db_symbol(symbol), Trade.closed_at.is_(None))
                .order_by(_desc(Trade.opened_at))
                .first()
            )

        if trade:
            trade.exit_price = exit_price
            trade.pnl = round(pnl, 2)
            trade.pnl_pct = round((pnl / (trade.entry_price * trade.quantity)) * 100 if trade.entry_price * trade.quantity > 0 else 0, 2)
            trade.exit_reason = "止损触发"
            trade.closed_at = datetime.now(timezone.utc)
            session.commit()
            log.info("trade_closed_stop_loss", symbol=symbol, pnl=pnl, trade_id=trade.id)
        else:
            log.warning("stop_loss_trade_not_found", symbol=symbol)

        # 通知风控模块
        record_stop_loss(exchange=exchange, session=session, pnl=pnl)

    except Exception as e:
        log.error("handle_stop_loss_error", symbol=symbol, error=str(e))
    finally:
        if own_session:
            session.close()


def handle_take_profit(
    symbol: str,
    exit_price: float,
    pnl: float,
    exit_reason: str = "止盈触发",
    trade_id: int | None = None,
    session: DBSession | None = None,
) -> None:
    """处理止盈后的善后：更新 trades 表 + 重置连续止损计数。"""
    own_session = False
    if session is None:
        session = get_session(settings.database_url)
        own_session = True

    try:
        trade = None
        if trade_id:
            trade = session.query(Trade).filter(Trade.id == trade_id).first()
        if trade is None:
            from sqlalchemy import desc as _desc
            trade = (
                session.query(Trade)
                .filter(Trade.symbol == to_db_symbol(symbol), Trade.closed_at.is_(None))
                .order_by(_desc(Trade.opened_at))
                .first()
            )

        if trade:
            trade.exit_price = exit_price
            trade.pnl = round(pnl, 2)
            trade.pnl_pct = round((pnl / (trade.entry_price * trade.quantity)) * 100 if trade.entry_price * trade.quantity > 0 else 0, 2)
            trade.exit_reason = exit_reason
            trade.closed_at = datetime.now(timezone.utc)
            session.commit()
            log.info("trade_closed_profit", symbol=symbol, pnl=pnl, reason=exit_reason)
        else:
            log.warning("take_profit_trade_not_found", symbol=symbol)

        # 重置连续止损计数
        from execution.risk_guard import record_profit
        record_profit(pnl=max(pnl, 0), session=session)

    except Exception as e:
        log.error("handle_take_profit_error", symbol=symbol, error=str(e))
    finally:
        if own_session:
            session.close()


def get_top_signals(limit: int = 3, session: DBSession | None = None) -> list[dict[str, Any]]:
    """获取最新一期得分最高的信号用于下单决策。

    Args:
        limit: 返回前 N 个信号
        session: 数据库 session

    Returns:
        信号列表 [{symbol, score_total, strategy_type, ...}]
    """
    own_session = False
    if session is None:
        session = get_session(settings.database_url)
        own_session = True

    try:
        from sqlalchemy import desc as _desc
        # 获取最新打分时间戳
        latest = session.query(SignalScore).order_by(_desc(SignalScore.captured_at)).first()
        if latest is None:
            return []

        cutoff = latest.captured_at
        top = (
            session.query(SignalScore)
            .filter(
                SignalScore.captured_at == cutoff,
                SignalScore.score_total >= settings.entry_threshold,
            )
            .order_by(_desc(SignalScore.score_total))
            .limit(limit)
            .all()
        )

        return [
            {
                "id": r.id,
                "symbol": r.symbol,
                "score_total": r.score_total,
                "strategy_type": r.strategy_type,
                "captured_at": r.captured_at.isoformat(),
            }
            for r in top
        ]
    except Exception as e:
        log.error("get_top_signals_failed", error=str(e))
        return []
    finally:
        if own_session:
            session.close()


def _get_price_precision(exchange: ccxt.binanceusdm, symbol: str) -> int:
    """获取交易所价格精度（小数位数）。"""
    try:
        market = exchange.market(symbol)
        raw = market.get("precision", {}).get("price", 2)
        # 如果是步长格式（如 1e-05），转换为小数位数
        if isinstance(raw, float) and raw < 1:
            import math
            return int(round(-math.log10(raw)))
        return int(raw)
    except Exception as e:
        log.warning("price_precision_failed", symbol=symbol, error=str(e))
        return 2
