"""仓位管理 — 资金分配 + 仓位计算 + 保证金管理

P1-⑤: ATR止损 + risk-based仓位（与回测v5一致）
- 止损距离 = 2 × ATR(14)
- 仓位 = equity × risk_per_trade × entry_price / (stop_distance × leverage)
- 删除固定20%分配 + get_stop_loss_amount 逻辑
"""

from dataclasses import dataclass

import ccxt
import structlog

from config.settings import settings

log = structlog.get_logger()


@dataclass
class PositionSizeResult:
    """仓位计算结果"""
    quantity: float
    margin: float
    position_value: float
    leverage: int
    risk_amount: float
    stop_loss_price: float
    stop_loss_distance: float  # P1-⑤: 止损价格距离 (price units)


def get_exchange() -> ccxt.binanceusdm:
    return ccxt.binanceusdm({
        "enableRateLimit": True,
        "proxies": settings.proxies,
        "options": {"defaultType": "future"},
        "timeout": 30000,
    })


def get_trading_exchange() -> ccxt.binanceusdm:
    return ccxt.binanceusdm({
        "enableRateLimit": True,
        "apiKey": settings.binance_api_key,
        "secret": settings.binance_api_secret,
        "proxies": settings.proxies,
        "options": {"defaultType": "future"},
        "timeout": 30000,
    })


def get_account_balance(exchange: ccxt.binanceusdm) -> float:
    try:
        balance = exchange.fetch_balance()
        return float(balance.get("USDT", {}).get("total", 0))
    except Exception as e:
        log.error("fetch_balance_failed", error=str(e))
        return 0.0


def get_stop_loss_amount(equity: float) -> float:
    """单笔止损金额（保留供风控模块使用，不再用于仓位计算）。

    早期（净值 <= 1000u）：账户净值 × 20%
    后期（净值 > 1000u）：固定 200u
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
    atr: float = 0.0,  # P1-⑤: 新增 ATR 参数
) -> PositionSizeResult | None:
    """计算某标的的开仓参数（P1-⑤: ATR止损 + risk-based仓位）。

    Args:
        exchange: ccxt 交易所实例
        symbol: 交易对
        equity: 当前账户权益
        entry_price: 入场价格
        strategy_type: 策略类型 A/B
        atr: ATR(14) 值 — 用于计算止损距离

    Returns:
        PositionSizeResult 或 None
    """
    if equity <= 0 or entry_price <= 0 or atr <= 0:
        raise ValueError(f"无效参数: equity={equity}, entry_price={entry_price}, atr={atr}")

    # 1. 选择杠杆
    leverage = (
        settings.leverage_strategy_a
        if strategy_type.upper() == "A"
        else settings.leverage_strategy_b
    )

    # 2. P1-⑤: ATR止损距离 + risk-based仓位
    stop_loss_distance = 2 * atr
    stop_loss_price = entry_price - stop_loss_distance

    # 仓位公式（与回测v5一致）：
    # size_pct = risk_per_trade × entry_price / (stop_distance × leverage)
    # position_value = equity × size_pct
    size_pct = settings.risk_per_trade * entry_price / (stop_loss_distance * leverage)
    size_pct = min(size_pct, 1.0)  # 不超过100%
    position_value = equity * size_pct
    risk_amount = position_value * (stop_loss_distance / entry_price) * leverage  # PnL@stop

    # 3. 计算数量
    quantity = position_value / entry_price
    try:
        market = exchange.market(symbol)
        qty_precision = market.get("precision", {}).get("amount", 8)
        quantity = _round_to_precision(quantity, qty_precision)
        min_amount = market.get("limits", {}).get("amount", {}).get("min", 0)
        if quantity < min_amount:
            log.warning("quantity_below_min", symbol=symbol, quantity=quantity, min=min_amount)
            return None
    except Exception as e:
        log.warning("market_info_failed", symbol=symbol, error=str(e))
        quantity = round(quantity, 6)
        if quantity <= 0:
            return None

    # 4. 计算保证金
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
        positions = exchange.fetch_positions()
        return sum(
            1 for pos in positions
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


def _round_to_precision(value: float, precision: int) -> float:
    factor = 10 ** precision
    return int(value * factor) / factor


def _get_price_precision(exchange: ccxt.binanceusdm, symbol: str) -> int:
    try:
        return exchange.market(symbol).get("precision", {}).get("price", 2)
    except Exception:
        return 2
