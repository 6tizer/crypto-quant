"""Tests for execution/risk_guard.py — 风控暂停/恢复逻辑."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from execution.risk_guard import (
    _calc_drawdown,
    _get_daily_loss_limit,
    check_risk_status,
    record_profit,
    record_stop_loss,
    reset_daily_loss,
)


def _mock_state(**overrides) -> MagicMock:
    """构建模拟 RiskState 对象."""
    defaults = {
        "id": 1,
        "consecutive_stops": 0,
        "daily_loss": 0.0,
        "total_drawdown_pct": 0.0,
        "is_paused": False,
        "pause_reason": "",
        "pause_until": None,
        "peak_equity": 0.0,
        "updated_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    state = MagicMock()
    for k, v in defaults.items():
        setattr(state, k, v)
    return state


def _default_mock_settings() -> MagicMock:
    """构建完整的 mock settings，覆盖 check_risk_status 所有属性访问."""
    ms = MagicMock()
    ms.max_consecutive_stops = 3
    ms.max_drawdown_pct = 30.0
    ms.fear_greed_pause_line = 15
    ms.risk_mode_threshold = 1000.0
    ms.daily_loss_limit = 500.0
    ms.daily_loss_limit_pct = 0.50
    ms.leverage_strategy_a = 5
    ms.leverage_strategy_b = 3
    return ms


# ── check_risk_status: 正常情况允许交易 ─────────────────────────


@patch("execution.risk_guard._get_equity", return_value=500.0)
@patch("execution.risk_guard._get_or_create_state")
@patch("execution.risk_guard.settings")
def test_check_risk_all_clear(mock_settings, mock_get_state, _mock_equity):
    """所有风控条件正常 → 允许交易."""
    mock_settings.max_consecutive_stops = 3
    mock_settings.max_drawdown_pct = 30.0
    mock_settings.fear_greed_pause_line = 15
    mock_settings.risk_mode_threshold = 1000.0
    mock_settings.daily_loss_limit = 500.0
    mock_settings.daily_loss_limit_pct = 0.50

    state = _mock_state(consecutive_stops=0, daily_loss=0, total_drawdown_pct=0, is_paused=False)
    mock_get_state.return_value = state

    with patch("execution.risk_guard._get_latest_fear_greed", return_value=None):
        allowed, reason = check_risk_status(session=MagicMock(), exchange=MagicMock())

    assert allowed is True
    assert reason == ""


# ── 连续止损暂停 ────────────────────────────────────────────────


@patch("execution.risk_guard._get_equity", return_value=500.0)
@patch("execution.risk_guard._get_or_create_state")
@patch("execution.risk_guard.settings")
def test_check_risk_consecutive_stops_triggers_pause(mock_settings, mock_get_state, _mock_equity):
    """连续止损 >= 阈值 → 暂停 24h."""
    mock_settings.max_consecutive_stops = 3
    mock_settings.max_drawdown_pct = 30.0
    mock_settings.fear_greed_pause_line = 15
    mock_settings.risk_mode_threshold = 1000.0
    mock_settings.daily_loss_limit = 500.0
    mock_settings.daily_loss_limit_pct = 0.50

    state = _mock_state(consecutive_stops=3, daily_loss=0, total_drawdown_pct=0, is_paused=False)
    mock_get_state.return_value = state

    with patch("execution.risk_guard._get_latest_fear_greed", return_value=None):
        with patch("notifications.tg.notify_risk_pause"):
            allowed, reason = check_risk_status(session=MagicMock(), exchange=MagicMock())

    assert allowed is False
    assert "连续止损" in reason
    assert state.is_paused is True


# ── 暂停期中（pause_until 未到期）─────────────────────────────────


@patch("execution.risk_guard._get_equity", return_value=500.0)
@patch("execution.risk_guard._get_or_create_state")
@patch("execution.risk_guard.settings")
def test_check_risk_pause_until_active(mock_settings, mock_get_state, _mock_equity):
    """is_paused=True 且 pause_until 未到期 → 继续暂停."""
    mock_settings.max_consecutive_stops = 3
    mock_settings.max_drawdown_pct = 30.0
    mock_settings.fear_greed_pause_line = 15
    mock_settings.risk_mode_threshold = 1000.0
    mock_settings.daily_loss_limit = 500.0
    mock_settings.daily_loss_limit_pct = 0.50

    future = datetime.now(timezone.utc) + timedelta(hours=12)
    state = _mock_state(is_paused=True, pause_reason="测试暂停", pause_until=future)
    mock_get_state.return_value = state

    allowed, reason = check_risk_status(session=MagicMock(), exchange=MagicMock())
    assert allowed is False
    assert reason == "测试暂停"


# ── 暂停到期自动恢复 ────────────────────────────────────────────


@patch("execution.risk_guard._get_equity", return_value=500.0)
@patch("execution.risk_guard._get_or_create_state")
@patch("execution.risk_guard.settings")
def test_check_risk_pause_expired_auto_resume(mock_settings, mock_get_state, _mock_equity):
    """is_paused=True 但 pause_until 已过期 → 自动恢复."""
    mock_settings.max_consecutive_stops = 3
    mock_settings.max_drawdown_pct = 30.0
    mock_settings.fear_greed_pause_line = 15
    mock_settings.risk_mode_threshold = 1000.0
    mock_settings.daily_loss_limit = 500.0
    mock_settings.daily_loss_limit_pct = 0.50

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    state = _mock_state(
        is_paused=True,
        pause_reason="过期暂停",
        pause_until=past,
        consecutive_stops=0,
        daily_loss=0,
        total_drawdown_pct=0,
    )
    mock_get_state.return_value = state

    with patch("execution.risk_guard._get_latest_fear_greed", return_value=None):
        allowed, reason = check_risk_status(session=MagicMock(), exchange=MagicMock())

    assert allowed is True
    assert state.is_paused is False


# ── 单日亏损暂停 ────────────────────────────────────────────────


@patch("execution.risk_guard._get_equity", return_value=2000.0)
@patch("execution.risk_guard._get_or_create_state")
@patch("execution.risk_guard.settings")
def test_check_risk_daily_loss_triggers_pause(mock_settings, mock_get_state, _mock_equity):
    """日亏损 >= 上限 → 暂停."""
    mock_settings.max_consecutive_stops = 3
    mock_settings.max_drawdown_pct = 30.0
    mock_settings.fear_greed_pause_line = 15
    mock_settings.risk_mode_threshold = 1000.0
    mock_settings.daily_loss_limit = 500.0
    mock_settings.daily_loss_limit_pct = 0.50

    state = _mock_state(
        consecutive_stops=0,
        daily_loss=550.0,
        total_drawdown_pct=0,
        is_paused=False,
    )
    mock_get_state.return_value = state

    with patch("execution.risk_guard._get_latest_fear_greed", return_value=None):
        allowed, reason = check_risk_status(session=MagicMock(), exchange=MagicMock())

    assert allowed is False
    assert "单日亏损" in reason


# ── 总回撤暂停（永久）───────────────────────────────────────────


@patch("execution.risk_guard._get_equity", return_value=500.0)
@patch("execution.risk_guard._get_or_create_state")
@patch("execution.risk_guard.settings")
def test_check_risk_drawdown_triggers_permanent_pause(mock_settings, mock_get_state, _mock_equity):
    """总回撤 >= 阈值 → 永久暂停（pause_until=None）."""
    mock_settings.max_consecutive_stops = 3
    mock_settings.max_drawdown_pct = 30.0
    mock_settings.fear_greed_pause_line = 15
    mock_settings.risk_mode_threshold = 1000.0
    mock_settings.daily_loss_limit = 500.0
    mock_settings.daily_loss_limit_pct = 0.50

    state = _mock_state(
        consecutive_stops=0,
        daily_loss=0,
        total_drawdown_pct=35.0,
        is_paused=False,
    )
    mock_get_state.return_value = state

    with patch("execution.risk_guard._get_latest_fear_greed", return_value=None):
        allowed, reason = check_risk_status(session=MagicMock(), exchange=MagicMock())

    assert allowed is False
    assert "总回撤" in reason


# ── 恐贪指数暂停 ────────────────────────────────────────────────


@patch("execution.risk_guard._get_equity", return_value=500.0)
@patch("execution.risk_guard._get_or_create_state")
@patch("execution.risk_guard.settings")
def test_check_risk_fear_greed_triggers_pause(mock_settings, mock_get_state, _mock_equity):
    """恐贪指数 <= 阈值 → 暂停."""
    mock_settings.max_consecutive_stops = 3
    mock_settings.max_drawdown_pct = 30.0
    mock_settings.fear_greed_pause_line = 15
    mock_settings.risk_mode_threshold = 1000.0
    mock_settings.daily_loss_limit = 500.0
    mock_settings.daily_loss_limit_pct = 0.50

    state = _mock_state(
        consecutive_stops=0,
        daily_loss=0,
        total_drawdown_pct=0,
        is_paused=False,
    )
    mock_get_state.return_value = state

    with patch("execution.risk_guard._get_latest_fear_greed", return_value=10):
        allowed, reason = check_risk_status(session=MagicMock(), exchange=MagicMock())

    assert allowed is False
    assert "恐贪" in reason


# ── _get_daily_loss_limit ───────────────────────────────────────


@patch("execution.risk_guard.settings")
def test_daily_loss_limit_early_stage(mock_settings):
    """净值 <= 1000u → equity × 50%."""
    mock_settings.risk_mode_threshold = 1000.0
    mock_settings.daily_loss_limit_pct = 0.50
    assert _get_daily_loss_limit(500.0) == 250.0


@patch("execution.risk_guard.settings")
def test_daily_loss_limit_late_stage(mock_settings):
    """净值 > 1000u → 固定 500u."""
    mock_settings.risk_mode_threshold = 1000.0
    mock_settings.daily_loss_limit = 500.0
    mock_settings.daily_loss_limit_pct = 0.50
    assert _get_daily_loss_limit(2000.0) == 500.0


# ── _calc_drawdown ──────────────────────────────────────────────


def test_calc_drawdown_new_high():
    """权益新高 → 回撤 0%，更新峰值."""
    dd, peak = _calc_drawdown(1500.0, 1000.0)
    assert dd == 0.0
    assert peak == 1500.0


def test_calc_drawdown_normal():
    """正常回撤计算."""
    dd, peak = _calc_drawdown(800.0, 1000.0)
    assert dd == 20.0
    assert peak == 1000.0


def test_calc_drawdown_zero_peak():
    """峰值为 0 → 回撤 0%."""
    dd, peak = _calc_drawdown(500.0, 0.0)
    assert dd == 0.0


# ── record_stop_loss ────────────────────────────────────────────


@patch("execution.risk_guard._get_equity", return_value=1000.0)
@patch("execution.risk_guard._get_or_create_state")
@patch("execution.risk_guard.settings")
def test_record_stop_loss_increments_counter(mock_settings, mock_get_state, _mock_equity):
    mock_settings.risk_mode_threshold = 1000.0
    mock_settings.leverage_strategy_a = 5
    mock_settings.daily_loss_limit = 500.0
    mock_settings.daily_loss_limit_pct = 0.50

    state = _mock_state(consecutive_stops=1, daily_loss=50.0)
    mock_get_state.return_value = state

    session = MagicMock()
    record_stop_loss(session=session, pnl=-30.0)

    assert state.consecutive_stops == 2
    assert state.daily_loss == 80.0
    session.commit.assert_called()


# ── record_profit ───────────────────────────────────────────────


@patch("execution.risk_guard._get_or_create_state")
@patch("execution.risk_guard.settings")
def test_record_profit_resets_consecutive_stops(mock_settings, mock_get_state):
    mock_settings.risk_mode_threshold = 1000.0
    mock_settings.daily_loss_limit = 500.0
    mock_settings.daily_loss_limit_pct = 0.50

    state = _mock_state(consecutive_stops=2)
    mock_get_state.return_value = state

    session = MagicMock()
    record_profit(pnl=50.0, session=session)

    assert state.consecutive_stops == 0
    session.commit.assert_called()


# ── reset_daily_loss ────────────────────────────────────────────


@patch("execution.risk_guard._get_or_create_state")
@patch("execution.risk_guard.settings")
def test_reset_daily_loss(mock_settings, mock_get_state):
    state = _mock_state(daily_loss=300.0)
    mock_get_state.return_value = state

    session = MagicMock()
    reset_daily_loss(session=session)

    assert state.daily_loss == 0.0
    session.commit.assert_called()
