"""仓位管理 — 资金分配 + 仓位计算 + 保证金管理"""

from dataclasses import dataclass

import ccxt
import structlog

from config.settings import settings

log = structlog.get_logger()


@dataclass
class PositionSizeResult:
    """仓位计算结果"""
    quantity: float          # 合约数量（张数/币数）
    margin: float            # 所需保证金 (USDT)
    position_value: float    # 仓位名义价值 (USDT)
    leverage: int            # 使用的杠杆倍数
    risk_amount: float       # 止损风险敞口 (USDT)
    stop_loss_price: float   # 止损价格


def get_exchange() -> ccxt.binanceusdm:
    """创建配置好代理和 API 密钥的交易所实例（只读账户不需要密钥）"""
    return ccxt.binanceusdm({
        "enableRateLimit": True,
        "proxies": settings.proxies,
        "options": {"defaultType": "future"},
        "timeout": 30000,
    })


def get_trading_exchange() -> ccxt.binanceusdm:
    """创建带 API 密钥的交易所实例（下单、查看持仓用）"""
    return ccxt.binanceusdm({
        "enableRateLimit": True,
        "apiKey": settings.binance_api_key,
        "secret": settings.binance_api_secret,
        "proxies": settings.proxies,
        "options": {"defaultType": "future"},
        "timeout": 30000,
    })


def get_account_balance(exchange: ccxt.binanceusdm) -> float:
    """获取 USDT 合约账户权益（钱包余额）"""
    try:
        balance = exchange.fetch_balance()
        total = float(balance.get("USDT", {}).get("total", 0))
        return total
    except Exception as e:
        log.error("fetch_balance_failed", error=str(e))
        return 0.0


def get_stop_loss_amount(equity: float) -> float:
    """根据账户净值计算单笔止损金额。

    早期（净值 <= 1000u）：账户净值 × 20%
    后期（净值 > 1000u）：固定 200u
    """
    if equity > settings.risk_mode_threshold:
        return settings.stop_loss_amount  # 200u
    return round(equity * settings.stop_loss_pct, 2)  # 20%


def calculate_position_size(
    exchange: ccxt.binanceusdm,
    symbol: str,
    equity: float,
    entry_price: float,
    strategy_type: str = "A",
) -> PositionSizeResult | None:
    """计算某标的的开仓参数。

    Args:
        exchange: ccxt 交易所实例
        symbol: 交易对，如 "BTC/USDT:USDT"
        equity: 当前账户权益
        entry_price: 入场价格
        strategy_type: 策略类型 A/B

    Returns:
        PositionSizeResult 或 None（计算失败）

    Raises:
        ValueError: 参数校验失败时
    """
    if equity <= 0 or entry_price <= 0:
        raise ValueError(f"无效参数: equity={equity}, entry_price={entry_price}")

    # 1. 选择杠杆
    leverage = (
        settings.leverage_strategy_a
        if strategy_type.upper() == "A"
        else settings.leverage_strategy_b
    )

    # 2. 计算仓位比例 — 单个标的不超过权益的 20%（1/5 最大持仓）
    position_pct = 0.20  # 单标的最大占用资金比例
    max_position_value = equity * position_pct * leverage
    position_value = min(max_position_value, equity * leverage * 0.5)

    # 3. 计算数量（需符合交易所精度）
    quantity = position_value / entry_price
    try:
        market = exchange.market(symbol)
        qty_precision = market.get("precision", {}).get("amount", 8)
        quantity = _round_to_precision(quantity, qty_precision)
        # 检查最小数量
        min_amount = market.get("limits", {}).get("amount", {}).get("min", 0)
        if quantity < min_amount:
            log.warning("quantity_below_min", symbol=symbol, quantity=quantity, min=min_amount)
            return None
    except Exception as e:
        log.warning("market_info_failed", symbol=symbol, error=str(e))
        # 保守地向下取整到 6 位小数
        quantity = round(quantity, 6)
        if quantity <= 0:
            return None

    # 4. 计算实际仓位价值和保证金
    actual_position_value = quantity * entry_price
    margin = actual_position_value / leverage

    # 5. 计算止损价格
    risk_amount = get_stop_loss_amount(equity)
    # 止损距离（价格单位）= 止损金额 / 数量（买入方向：价格向下）
    stop_distance = risk_amount / quantity if quantity > 0 else 0
    stop_loss_price = round(entry_price - stop_distance, max(2, _get_price_precision(exchange, symbol)))

    return PositionSizeResult(
        quantity=quantity,
        margin=round(margin, 2),
        position_value=round(actual_position_value, 2),
        leverage=leverage,
        risk_amount=risk_amount,
        stop_loss_price=stop_loss_price,
    )


def get_open_position_count(exchange: ccxt.binanceusdm) -> int:
    """获取当前持有仓位数量（仅做多）"""
    try:
        positions = exchange.fetch_positions()
        long_count = 0
        for pos in positions:
            size = float(pos.get("contracts", 0) or 0)
            side = str(pos.get("side", ""))
            if size > 0 and side.upper() == "LONG":
                long_count += 1
        return long_count
    except Exception as e:
        log.error("fetch_positions_failed", error=str(e))
        return 0


def can_open_new_position(exchange: ccxt.binanceusdm, max_positions: int = 5) -> bool:
    """检查是否还能开新仓。

    Args:
        exchange: ccxt 交易所实例
        max_positions: 最大同时持仓数（3-5）

    Returns:
        True 可以开仓，False 已达上限
    """
    current = get_open_position_count(exchange)
    if current >= max_positions:
        log.warning("max_positions_reached", current=current, max=max_positions)
        return False
    return True


def _round_to_precision(value: float, precision: int) -> float:
    """按交易所精度向下取整"""
    factor = 10**precision
    return int(value * factor) / factor


def _get_price_precision(exchange: ccxt.binanceusdm, symbol: str) -> int:
    """获取价格精度"""
    try:
        market = exchange.market(symbol)
        return market.get("precision", {}).get("price", 2)
    except Exception:
        return 2
