"""TG 通知模块 — 开仓/平仓/风控事件推送到 Telegram

用法:
    from notifications.tg import send_notification
    send_notification("🚀 BTC/USDT 开多 | 0.50分 | 止损85600")
"""

import httpx
import structlog

from config.settings import settings

log = structlog.get_logger()

TG_API = "https://api.telegram.org"


def send_notification(text: str) -> bool:
    """发送通知到配置的 TG chat。

    Returns:
        True 成功，False 失败
    """
    token = settings.tg_bot_token
    chat_id = settings.tg_chat_id

    if not token or not chat_id:
        log.warning("tg_not_configured")
        return False

    try:
        url = f"{TG_API}/bot{token}/sendMessage"
        resp = httpx.post(
            url,
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("tg_sent", text=text[:80])
            return True
        else:
            log.error("tg_send_failed", status=resp.status_code, body=resp.text[:200])
            return False
    except Exception as e:
        log.error("tg_send_error", error=str(e))
        return False


def notify_trade_open(symbol: str, score: float, entry_price: float, stop_loss: float) -> None:
    """通知开仓。"""
    send_notification(
        f"🟢 <b>开仓</b> {symbol}\n"
        f"分数: {score:.2f} | 入场: {entry_price:.4f}\n"
        f"止损: {stop_loss:.4f}"
    )


def notify_trade_close(symbol: str, pnl: float, reason: str) -> None:
    """通知平仓。"""
    emoji = "🟢" if pnl > 0 else "🔴"
    send_notification(
        f"{emoji} <b>平仓</b> {symbol}\n"
        f"盈亏: {pnl:+.2f}u | 原因: {reason}"
    )


def notify_stop_loss(symbol: str, loss: float) -> None:
    """通知止损。"""
    send_notification(f"🛑 <b>止损</b> {symbol} | 亏损: {loss:.2f}u")


def notify_risk_pause(reason: str) -> None:
    """通知风控暂停。"""
    send_notification(f"⛔ <b>风控暂停</b>\n{reason}")
