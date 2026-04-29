"""风控守卫 — 连续止损、日亏、总回撤、恐贪检查

从 risk_state 表读取/写入状态，外部模块调用 check_risk_status() 判断能否交易。
"""

from datetime import datetime, timedelta, timezone

import ccxt
import structlog
from sqlalchemy import desc
from sqlalchemy.orm import Session as DBSession

from config.settings import settings
from data.db.models import FearGreedHistory, RiskState, get_session
from execution.portfolio import get_account_balance, get_trading_exchange

log = structlog.get_logger()


# ============================================================
# 核心接口
# ============================================================


def check_risk_status(
    exchange: ccxt.binanceusdm | None = None,
    session: DBSession | None = None,
) -> tuple[bool, str]:
    """综合风控检查 — 所有暂停条件逐一判断。

    Returns:
        (allowed: bool, reason: str)
            allowed=True  → 可以交易
            allowed=False → 原因见 reason
    """
    own_session = False
    if session is None:
        session = get_session(settings.database_url)
        own_session = True

    try:
        # 0. 从 DB 获取当前风控状态
        state = _get_or_create_state(session)

        # 1. 检查是否处于暂停期（pause_until > now）
        if state.is_paused and state.pause_until:
            if datetime.now(timezone.utc) < state.pause_until:
                remaining = (state.pause_until - datetime.now(timezone.utc)).total_seconds()
                log.warning("trading_paused_active", reason=state.pause_reason, remaining_secs=int(remaining))
                return False, state.pause_reason
            else:
                # 暂停到期，自动恢复
                state.is_paused = False
                state.pause_reason = ""
                state.pause_until = None
                session.commit()
                log.info("trading_resumed_auto")

        # 2. 检查连续止损
        if state.consecutive_stops >= settings.max_consecutive_stops:
            pause_until = datetime.now(timezone.utc) + timedelta(hours=24)
            state.is_paused = True
            state.pause_reason = f"连续止损 {state.consecutive_stops} 次 >= {settings.max_consecutive_stops}，暂停 24h"
            state.pause_until = pause_until
            session.commit()
            log.warning("paused_consecutive_stops", consecutive=state.consecutive_stops, until=pause_until.isoformat())
            from notifications.tg import notify_risk_pause
            notify_risk_pause(state.pause_reason)
            return False, state.pause_reason

        # 3. 检查单日亏损
        equity = _get_equity(exchange)
        daily_limit = _get_daily_loss_limit(equity)
        if state.daily_loss >= daily_limit:
            pause_until = datetime.now(timezone.utc) + timedelta(hours=24)
            state.is_paused = True
            state.pause_reason = f"单日亏损 {state.daily_loss:.2f}u >= 上限 {daily_limit:.2f}u，暂停 24h"
            state.pause_until = pause_until
            session.commit()
            log.warning("paused_daily_loss", daily_loss=state.daily_loss, limit=daily_limit)
            return False, state.pause_reason

        # 4. 检查总回撤
        if state.total_drawdown_pct >= settings.max_drawdown_pct:
            state.is_paused = True
            state.pause_reason = f"总回撤 {state.total_drawdown_pct:.1f}% >= {settings.max_drawdown_pct}%，全停"
            state.pause_until = None  # 永久暂停，人工恢复
            session.commit()
            log.critical("paused_drawdown", drawdown=state.total_drawdown_pct, limit=settings.max_drawdown_pct)
            return False, state.pause_reason

        # 5. 检查恐贪指数
        fg_value = _get_latest_fear_greed(session)
        if fg_value is not None and fg_value <= settings.fear_greed_pause_line:
            state.is_paused = True
            state.pause_reason = f"恐贪指数 {fg_value} <= {settings.fear_greed_pause_line}，全停"
            state.pause_until = None  # 永久暂停，人工恢复
            session.commit()
            log.warning("paused_fear_greed", fear_greed=fg_value, limit=settings.fear_greed_pause_line)
            return False, state.pause_reason

        return True, ""

    except Exception as e:
        log.error("risk_check_failed", error=str(e))
        # 异常时保守暂停
        return False, f"风控检查异常: {e}"
    finally:
        if own_session:
            session.close()


def record_stop_loss(
    exchange: ccxt.binanceusdm | None = None,
    session: DBSession | None = None,
    pnl: float = 0,
) -> None:
    """记录一次止损，更新连续止损计数和日亏累计。

    Args:
        exchange: 交易所实例（用于获取权益）
        session: 数据库 session
        pnl: 本次盈亏金额 (USDT)，负值为亏损
    """
    own_session = False
    if session is None:
        session = get_session(settings.database_url)
        own_session = True

    try:
        state = _get_or_create_state(session)

        # 增加连续止损
        state.consecutive_stops += 1

        # 累计日亏损（仅记负数部分）
        if pnl < 0:
            state.daily_loss += abs(pnl)

        # 检查总回撤
        equity = _get_equity(exchange)
        if equity > 0:
            drawdown, new_peak = _calc_drawdown(equity, state.peak_equity or 0)
            state.total_drawdown_pct = drawdown
            state.peak_equity = new_peak  # P1-⑦

        state.updated_at = datetime.now(timezone.utc)
        session.commit()
        log.info("stop_loss_recorded", consecutive=state.consecutive_stops, daily_loss=state.daily_loss)
    except Exception as e:
        session.rollback()
        log.error("record_stop_loss_failed", error=str(e))
    finally:
        if own_session:
            session.close()


def record_profit(
    pnl: float = 0,
    session: DBSession | None = None,
) -> None:
    """记录一笔盈利交易，重置连续止损计数。"""
    own_session = False
    if session is None:
        session = get_session(settings.database_url)
        own_session = True

    try:
        state = _get_or_create_state(session)
        state.consecutive_stops = 0  # 盈利即重置连续止损
        state.updated_at = datetime.now(timezone.utc)
        session.commit()
        log.info("profit_recorded", consecutive_reset=True)
    except Exception as e:
        log.error("record_profit_failed", error=str(e))
    finally:
        if own_session:
            session.close()


def reset_daily_loss(session: DBSession | None = None) -> None:
    """每日归零日亏损 — 应在每天 00:00 UTC 调用。"""
    own_session = False
    if session is None:
        session = get_session(settings.database_url)
        own_session = True

    try:
        state = _get_or_create_state(session)
        state.daily_loss = 0.0
        state.updated_at = datetime.now(timezone.utc)
        session.commit()
        log.info("daily_loss_reset")
    except Exception as e:
        log.error("daily_loss_reset_failed", error=str(e))
    finally:
        if own_session:
            session.close()


def update_drawdown(
    exchange: ccxt.binanceusdm | None = None,
    session: DBSession | None = None,
) -> float:
    """更新总回撤百分比并写入 DB。返回当前回撤值。"""
    own_session = False
    if session is None:
        session = get_session(settings.database_url)
        own_session = True

    try:
        equity = _get_equity(exchange)
        if equity <= 0:
            return 0.0

        state = _get_or_create_state(session)
        drawdown, new_peak = _calc_drawdown(equity, state.peak_equity or 0)
        state.total_drawdown_pct = round(drawdown, 2)
        state.peak_equity = new_peak  # P1-⑦
        state.updated_at = datetime.now(timezone.utc)
        session.commit()

        if drawdown > 20:
            log.warning("drawdown_high", drawdown=drawdown)

        return drawdown
    except Exception as e:
        log.error("update_drawdown_failed", error=str(e))
        return 0.0
    finally:
        if own_session:
            session.close()


# ============================================================
# 内部辅助
# ============================================================


def _get_or_create_state(session: DBSession) -> RiskState:
    """获取最新风控状态记录，不存在则创建。"""
    state = session.query(RiskState).order_by(desc(RiskState.id)).first()
    if state is None:
        state = RiskState(
            consecutive_stops=0,
            daily_loss=0.0,
            total_drawdown_pct=0.0,
            is_paused=False,
            pause_reason="",
            pause_until=None,
            peak_equity=0.0,  # P1-⑦
            updated_at=datetime.now(timezone.utc),
        )
        session.add(state)
        session.flush()
    return state


def _get_equity(exchange: ccxt.binanceusdm | None = None) -> float:
    """获取当前账户权益。"""
    if exchange is None:
        try:
            exchange = get_trading_exchange()
        except Exception:
            return 0.0
    try:
        return get_account_balance(exchange)
    except Exception:
        return 0.0


def _get_daily_loss_limit(equity: float) -> float:
    """计算当日亏损上限。

    早期（净值 <= 1000u）：账户净值 × 50%
    后期（净值 > 1000u）：固定 500u
    """
    if equity > settings.risk_mode_threshold:
        return settings.daily_loss_limit  # 500u
    return round(equity * settings.daily_loss_limit_pct, 2)  # 50%


def _calc_drawdown(equity: float, peak_equity: float = 0) -> tuple[float, float]:
    """P1-⑦: peak-to-trough 回撤。

    Returns:
        (drawdown_pct, new_peak_equity)
    """
    if equity > peak_equity:
        return 0.0, equity
    if peak_equity <= 0:
        return 0.0, max(equity, peak_equity)
    drawdown = (peak_equity - equity) / peak_equity * 100
    return round(drawdown, 2), peak_equity


def _get_latest_fear_greed(session: DBSession) -> int | None:
    """从数据库读取最新恐贪指数值。"""
    try:
        row = (
            session.query(FearGreedHistory)
            .order_by(desc(FearGreedHistory.timestamp))
            .first()
        )
        return row.value if row else None
    except Exception:
        return None
