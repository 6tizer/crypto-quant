"""Tests for execution/portfolio.py — 仓位计算 + 精度处理."""

from unittest.mock import MagicMock, patch

import pytest

from execution.portfolio import (
    PositionSizeResult,
    _precision_to_step,
    _round_to_precision,
    calculate_position_size,
)


# ── _precision_to_step ──────────────────────────────────────────


def test_precision_to_step_tick_size_float():
    """TICK_SIZE 模式：precision.amount=0.01 → 步长 0.01."""
    assert _precision_to_step(0.01) == 0.01


def test_precision_to_step_tick_size_one():
    """precision.amount=1.0 → 步长 1.0（整数步长）."""
    assert _precision_to_step(1.0) == 1.0


def test_precision_to_step_decimal_digits():
    """旧式 DECIMAL_DIGITS：precision=2 → 步长 0.01."""
    assert _precision_to_step(2) == pytest.approx(0.01, abs=1e-10)


def test_precision_to_step_none():
    """precision=None → 回退到 1e-8."""
    assert _precision_to_step(None) == 1e-8


def test_precision_to_step_zero():
    """precision=0 → 回退到 1.0."""
    assert _precision_to_step(0) == 1.0


def test_precision_to_step_negative():
    """负数 → 回退到 1.0."""
    assert _precision_to_step(-1) == 1.0


# ── _round_to_precision ─────────────────────────────────────────


def test_round_to_precision_tick_size():
    """步长 0.01 → floor 到 0.01 倍数."""
    assert _round_to_precision(1.234, 0.01) == 1.23


def test_round_to_precision_exact():
    """恰好是步长倍数，不变."""
    assert _round_to_precision(1.23, 0.01) == 1.23


def test_round_to_precision_integer_step():
    """步长 1.0 → floor 到整数."""
    assert _round_to_precision(3.7, 1.0) == 3.0


def test_round_to_precision_decimal_digits_mode():
    """旧式 precision=2（两位小数）→ floor 到 0.01 倍数."""
    assert _round_to_precision(1.234, 2) == pytest.approx(1.23, abs=1e-10)


def test_round_to_precision_none():
    """precision=None → 不截断（step=1e-8，几乎不变）."""
    val = _round_to_precision(1.23456789, None)
    assert abs(val - 1.23456789) < 1e-6


# ── calculate_position_size ─────────────────────────────────────


def _mock_exchange(
    symbol: str = "BTC/USDT:USDT",
    amount_precision: float = 0.01,
    price_precision: float = 0.01,
    min_amount: float = 0.001,
    min_cost: float = 5.0,
) -> MagicMock:
    """构建一个模拟 ccxt exchange 实例."""
    exchange = MagicMock()
    exchange.market.return_value = {
        "precision": {"amount": amount_precision, "price": price_precision},
        "limits": {
            "amount": {"min": min_amount},
            "cost": {"min": min_cost},
        },
    }
    return exchange


@patch("execution.portfolio.settings")
def test_calculate_position_size_basic(mock_settings):
    mock_settings.risk_per_trade = 0.02
    mock_settings.leverage_strategy_a = 5

    exchange = _mock_exchange("BTC/USDT:USDT", amount_precision=0.001)
    result = calculate_position_size(
        exchange=exchange,
        symbol="BTC/USDT:USDT",
        equity=1000.0,
        entry_price=50000.0,
        atr=500.0,
        strategy_type="A",
    )

    assert result is not None
    assert result.quantity > 0
    assert result.stop_loss_price < 50000.0
    assert result.leverage == 5
    assert result.position_value >= 5.0  # min notional


@patch("execution.portfolio.settings")
def test_calculate_position_size_no_infinite_loop(mock_settings):
    """INTC 案例复现：TICK_SIZE 模式下 quantity=0 死循环必须被 guard 拦截."""
    mock_settings.risk_per_trade = 0.02
    mock_settings.leverage_strategy_a = 5

    # 模拟 INTC：amount precision=0.01, price=25.0, 极小 equity
    exchange = _mock_exchange("INTC/USDT:USDT", amount_precision=0.01, min_amount=1.0)
    result = calculate_position_size(
        exchange=exchange,
        symbol="INTC/USDT:USDT",
        equity=100.0,
        entry_price=25.0,
        atr=0.5,
        strategy_type="A",
    )

    # 关键：函数必须返回（不死循环）
    assert result is not None or result is None  # 只要没 hang 就行
    if result is not None:
        assert result.quantity > 0


@patch("execution.portfolio.settings")
def test_calculate_position_size_quantity_within_bounds(mock_settings):
    mock_settings.risk_per_trade = 0.02
    mock_settings.leverage_strategy_a = 5

    exchange = _mock_exchange("BTC/USDT:USDT", amount_precision=0.001)
    result = calculate_position_size(
        exchange=exchange,
        symbol="BTC/USDT:USDT",
        equity=1000.0,
        entry_price=50000.0,
        atr=1000.0,
        strategy_type="A",
    )

    assert result is not None
    # quantity 必须对齐步长
    step = _precision_to_step(0.001)
    remainder = round(result.quantity % step, 10)
    assert remainder == pytest.approx(0.0, abs=1e-8)


def test_calculate_position_size_invalid_equity():
    exchange = _mock_exchange()
    with pytest.raises(ValueError, match="无效参数"):
        calculate_position_size(exchange, "BTC/USDT:USDT", equity=0, entry_price=100, atr=1)


def test_calculate_position_size_invalid_price():
    exchange = _mock_exchange()
    with pytest.raises(ValueError, match="无效参数"):
        calculate_position_size(exchange, "BTC/USDT:USDT", equity=100, entry_price=0, atr=1)


def test_calculate_position_size_invalid_atr():
    exchange = _mock_exchange()
    with pytest.raises(ValueError, match="无效参数"):
        calculate_position_size(exchange, "BTC/USDT:USDT", equity=100, entry_price=100, atr=0)


@patch("execution.portfolio.settings")
def test_calculate_position_size_strategy_b_leverage(mock_settings):
    mock_settings.risk_per_trade = 0.02
    mock_settings.leverage_strategy_a = 5
    mock_settings.leverage_strategy_b = 3

    exchange = _mock_exchange("BTC/USDT:USDT", amount_precision=0.001)
    result = calculate_position_size(
        exchange=exchange,
        symbol="BTC/USDT:USDT",
        equity=1000.0,
        entry_price=50000.0,
        atr=500.0,
        strategy_type="B",
    )

    assert result is not None
    assert result.leverage == 3
