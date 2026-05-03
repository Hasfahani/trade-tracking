"""Telegram alert helpers for trade notifications."""

import logging

import httpx
from sqlalchemy.orm import Session

from app.models import AppSettings, Trade, Wallet

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org"


def get_app_settings(db: Session) -> AppSettings:
    settings = db.query(AppSettings).filter(AppSettings.id == 1).first()
    if settings is None:
        settings = AppSettings(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def _short_address(address: str) -> str:
    if len(address) >= 10:
        return f"{address[:6]}…{address[-4:]}"
    return address


def _build_message(trade: Trade, wallet: Wallet) -> str:
    label = wallet.label or _short_address(wallet.address)
    direction = "BUY (YES)" if trade.side == "YES" else "SELL (NO)"
    value = trade.price * trade.size
    market = trade.market_title or trade.condition_id
    return (
        f"🔔 <b>Trade Alert</b>\n"
        f"<b>Wallet:</b> {label}\n"
        f"<b>Market:</b> {market}\n"
        f"<b>Direction:</b> {direction}\n"
        f"<b>Size:</b> {trade.size:.2f} shares\n"
        f"<b>Value:</b> ${value:.2f}\n"
        f"<b>Price:</b> ${trade.price:.4f}"
    )


def send_telegram_message(token: str, chat_id: str, text: str) -> bool:
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                f"{_TELEGRAM_API}/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            )
        if not resp.is_success:
            logger.warning("Telegram sendMessage failed: %s %s", resp.status_code, resp.text[:200])
            return False
        return True
    except Exception as exc:
        logger.warning("Telegram sendMessage error: %s", exc)
        return False


def fire_alerts_for_new_trades(db: Session, wallet: Wallet, after_id: int) -> int:
    """Send alerts for trades inserted during this refresh that exceed the size threshold.

    after_id: the max Trade.id for this wallet captured before the refresh; only rows
    with id > after_id are considered new. Returns the number of alerts sent.
    """
    settings = get_app_settings(db)
    if not settings.alerts_enabled:
        return 0

    token = (settings.telegram_bot_token or "").strip()
    chat_id = (settings.telegram_chat_id or "").strip()
    if not token or not chat_id:
        return 0

    threshold = float(settings.alert_min_size or 0.0)
    new_trades = (
        db.query(Trade)
        .filter(
            Trade.wallet_address == wallet.address,
            Trade.id > after_id,
            Trade.size >= threshold,
        )
        .all()
    )

    sent = 0
    for trade in new_trades:
        text = _build_message(trade, wallet)
        if send_telegram_message(token, chat_id, text):
            sent += 1
    return sent
