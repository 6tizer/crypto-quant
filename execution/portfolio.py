"""仓位管理 — 资金分配 + 仓位计算 + 保证金管理

P1-⑤: ATR止损 + risk-based仓位（与回测v5一致）
"""

from dataclasses import dataclass
import math

import ccxt
import structlog

from config.settings import settings

log = structlog.get_logger()


@dataclass
class PositionSizeResult:
    quantity: float
    margin: float
    position_value: float
    leverage: int
    risk_amount: float
    stop_loss_price: float
    stop_loss_distance: float


def _build_exchange(api_key: str = "", secret: str = "") -> ccxt.binanceusdm:
    """构建交易所实例。Demo 模式自动启用 enable_demo_trading。
    
    Demo Trading 已知限制：binanceusdm v2/v3 账户端点返回 -1109，
    需使用 v1 端点。余额通过 positions + usdt_balance 自算。
    """
    params = {
        "enableRateLimit": True,
        "proxies": settings.proxies,
        "options": {"defaultType": "future"},
        "timeout": 30000,
    }
    if api_key:
        params["apiKey"] = api_key
    if secret:
        params["secret"] = secret
    
    ex = ccxt.binanceusdm(params)
    
    if settings.binance_demo_trading and api_key and secret:
        ex.enable_demo_trading(True)
    
    return ex


def get_exchange() -> ccxt.binanceusdm:
    return _build_exchange()


def get_trading_exchange() -> ccxt.binanceusdm:
    return _build_exchange(
        api_key=settings.binance_api_key,
        secret=settings.binance_api_secret,
    )


def get_account_balance(exchange: ccxt.binanceusdm) -> float:
    """获取账户余额。Demo 模式从 DB 自算（API 不可用）。"""
    if settings.binance_demo_trading:
        return _get_balance_from_db()
    
    try:
        balance = exchange.fetch_balance()
        return float(balance.get("USDT", {}).get("total", 0))
    except Exception as e:
        log.error("fetch_balance_failed", error=str(e))
        return _get_balance_from_db()


def _get_balance_from_db() -> float:
    """从 trades 表自算当前余额（Demo 模式 fallback）。

    计算方式: 初始资金 + 已平仓PnL - 未平仓持仓保证金
    """
    from data.db.models import Trade, get_session
    session = get_session(settings.database_url)
    try:
        trades = session.query(Trade).filter(
            Trade.closed_at.isnot(None)
        ).all()
        total_pnl = sum(t.pnl for t in trades)

        # 扣除未平仓持仓占用的保证金
        open_trades = session.query(Trade).filter(
            Trade.closed_at.is_(None)
        ).all()
        used_margin = sum(
            t.quantity * t.entry_price / settings.leverage_strategy_a
            for t in open_trades
            if t.quantity and t.entry_price
        )

        return settings.initial_capital + total_pnl - used_margin
    except Exception as e:
        log.error("balance_calc_failed", error=str(e))
        return settings.initial_capital
    finally:
        session.close()


def get_stop_loss_amount(equity: float) -> float:
    """单笔止损金额（保留供风控引用）。

    早期: equity × 20%  |  后期: 固定 200u
    """
    if equity > settings.risk_mode_threshold:
        return settings.stop_loss_amount
    return round(equity * settings.stop_loss_pct, 2)


def calculate_position_size(
    exchange: ccxt.binanceusdm,
    symbol: str,
    equity: float,
    entry_price: float,
    strategy_type: str = "A",
    atr: float = 0.0,
) -> PositionSizeResult | None:
    """计算开仓参数 — ATR止损 + risk-based仓位。"""
    if equity <= 0 or entry_price <= 0 or atr <= 0:
        raise ValueError(f"无效参数: equity={equity}, entry_price={entry_price}, atr={atr}")

    leverage = (
        settings.leverage_strategy_a
        if strategy_type.upper() == "A"
        else settings.leverage_strategy_b
    )

    stop_loss_distance = 2 * atr
    stop_loss_price = entry_price - stop_loss_distance

    # size_pct = risk_per_trade × entry_price / (stop_distance × leverage)
    size_pct = settings.risk_per_trade * entry_price / (stop_loss_distance * leverage)
    size_pct = min(size_pct, 1.0)
    position_value = equity * size_pct
    # 币安合约最低名义金额 5 USDT
    MIN_NOTIONAL = 5.0
    if position_value < MIN_NOTIONAL:
        log.info("bumping_to_min_notional", original=round(position_value, 2), min=MIN_NOTIONAL)
        position_value = MIN_NOTIONAL
        size_pct = position_value / equity
    risk_amount = position_value * (stop_loss_distance / entry_price) * leverage

    quantity = position_value / entry_price
    try:
        market = exchange.market(symbol)
        qty_precision = market.get("precision", {}).get("amount", 8)
        min_amount = market.get("limits", {}).get("amount", {}).get("min", 0)
        min_cost = market.get("limits", {}).get("cost", {}).get("min", 0)
        # 先算最低数量（满足 min_cost 和 min_notional，加 10% buffer 防价格波动）
        effective_min = max(MIN_NOTIONAL, min_cost or 0) * 1.10
        min_qty_for_cost = effective_min / entry_price if entry_price > 0 else 0
        quantity = max(quantity, min_qty_for_cost, min_amount or 0)
        quantity = _round_to_precision(quantity, qty_precision)
        # 舍入后如果金额不够，向上补一步。ccxt 在 TICK_SIZE 模式下 precision.amount 是步长（如 0.01），不是小数位数。
        step = _precision_to_step(qty_precision)
        max_iterations = 1000
        iterations = 0
        while quantity * entry_price < effective_min / 1.10 and quantity < 1e12:
            quantity = _round_to_precision(quantity + step, qty_precision)
            iterations += 1
            if iterations >= max_iterations:
                log.error(
                    "quantity_rounding_loop_guard_triggered",
                    symbol=symbol,
                    quantity=quantity,
                    step=step,
                    qty_precision=qty_precision,
                    entry_price=entry_price,
                    effective_min=effective_min,
                )
                return None
        if quantity < (min_amount or 0):
            log.warning("quantity_below_min", symbol=symbol, quantity=quantity, min=min_amount)
            return None
    except Exception as e:
        log.warning("market_info_failed", symbol=symbol, error=str(e))
        quantity = round(quantity, 6)
        if quantity <= 0:
            return None

    actual_position_value = quantity * entry_price
    margin = actual_position_value / leverage

    return PositionSizeResult(
        quantity=quantity,
        margin=round(margin, 2),
        position_value=round(actual_position_value, 2),
        leverage=leverage,
        risk_amount=round(risk_amount, 2),
        stop_loss_price=round(stop_loss_price, 6),
        stop_loss_distance=round(stop_loss_distance, 6),
    )


def get_open_position_count(exchange: ccxt.binanceusdm) -> int:
    try:
        return sum(
            1 for pos in exchange.fetch_positions()
            if float(pos.get("contracts", 0) or 0) > 0
            and str(pos.get("side", "")).upper() == "LONG"
        )
    except Exception as e:
        log.error("fetch_positions_failed", error=str(e))
        return 0


def can_open_new_position(exchange: ccxt.binanceusdm, max_positions: int = 5) -> bool:
    current = get_open_position_count(exchange)
    if current >= max_positions:
        log.warning("max_positions_reached", current=current, max=max_positions)
        return False
    return True


def _precision_to_step(precision: int | float | None) -> float:
    """把 ccxt amount precision 转成数量步长。

    Binance/ccxt 当前是 TICK_SIZE 模式：precision.amount 可能是 0.01、1.0。
    旧代码把 0.01 当成“小数位数”转 int，导致 quantity 永远 round 成 0 并死循环。
    """
    if precision is None:
        return 1e-8
    raw = float(precision)
    if raw <= 0:
        return 1.0
    if raw < 1:
        return raw
    # 在 TICK_SIZE 模式下 1.0 表示整数步长；兼容旧式 precision=2 表示两位小数。
    if raw == 1.0:
        return 1.0
    return 10 ** (-int(raw))


def _round_to_precision(value: float, precision: int | float | None) -> float:
    step = _precision_to_step(precision)
    if step <= 0:
        return value
    rounded = math.floor(value / step) * step
    # 避免 1.2300000000000002 这类浮点噪音
    decimals = max(0, int(round(-math.log10(step)))) if step < 1 else 0
    return round(rounded, decimals)


def _get_price_precision(exchange: ccxt.binanceusdm, symbol: str) -> int:
    try:
        raw = exchange.market(symbol).get("precision", {}).get("price", 2)
        # 如果是步长格式（如 1e-05），转换为小数位数
        if isinstance(raw, float) and raw < 1:
            import math
            return int(round(-math.log10(raw)))
        return int(raw)
    except Exception as e:
        log.warning("price_precision_failed", symbol=symbol, error=str(e))
        return 2
