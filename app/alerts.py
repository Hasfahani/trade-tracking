# Summary: Sends Telegram alerts for new trades.
# Details: It supports the FastAPI backend by keeping one main piece of app behavior clear and reusable.
"""Telegram alert helpers for trade notifications."""

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy.orm import Session

from app.models import AppSettings, Trade, Wallet

logger = logging.getLogger(__name__)

_TELEGRAM_API = "https://api.telegram.org"
_DEFAULT_MIN_VALUE = 1000.0   # dollars (price * size) - alert on trades this big or larger
# A $1000 threshold keeps the chat to genuinely large trades; at lower values too
# many trades qualify per refresh. The per-wallet cap still suppresses over-cap
# trades to avoid runaway spam on a busy refresh.
_MAX_ALERTS_PER_WALLET = 10
_LOOKBACK_HOURS = 24

# Default Telegram credentials baked in so a plain code deploy is self-configuring.
# An env var (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) overrides these without a code
# edit, so the token can be rotated from the Render dashboard after a /revoke.
_DEFAULT_BOT_TOKEN = "8648652719:AAFZmxQFWYV3T5CRMx01VrObhYxIHkBGs14"
_DEFAULT_CHAT_ID = "8708428862"


def seed_telegram_settings(db: Session) -> bool:
    """Ensure the app_settings row carries working Telegram credentials.

    Reads the token/chat ID from the environment when present, otherwise falls
    back to the baked-in defaults, then writes them onto the settings row and
    enables alerts. Idempotent: only commits when something actually changes, so
    it is safe to run on every startup. Returns True if the row was updated.
    """
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or _DEFAULT_BOT_TOKEN).strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or _DEFAULT_CHAT_ID).strip()
    if not token or not chat_id:
        return False

    settings = get_app_settings(db)
    # Was Telegram already configured before this run? If not, this is the very
    # first setup and we baseline the existing trade backlog below so the first
    # refresh doesn't fire an alert for every historical trade at once.
    first_time = not (settings.telegram_bot_token or "").strip()
    changed = False
    if (settings.telegram_bot_token or "").strip() != token:
        settings.telegram_bot_token = token
        changed = True
    if (settings.telegram_chat_id or "").strip() != chat_id:
        settings.telegram_chat_id = chat_id
        changed = True
    if not settings.alerts_enabled:
        settings.alerts_enabled = 1
        changed = True
    if not settings.alert_min_size:
        settings.alert_min_size = _DEFAULT_MIN_VALUE
        changed = True

    if first_time:
        # Mark every pre-existing unsent trade as already-alerted so going live
        # never dumps the whole backlog into the chat on the first refresh. Only
        # trades ingested after this point will ever fire an alert.
        baselined = (
            db.query(Trade)
            .filter(Trade.alert_sent == 0)
            .update({"alert_sent": 1}, synchronize_session=False)
        )
        if baselined:
            changed = True
            logger.info(
                "Startup maintenance: baselined %d existing trades (no alert on first refresh)",
                baselined,
            )

    if changed:
        settings.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        logger.info("Startup maintenance: seeded Telegram alert settings (chat_id=%s)", chat_id)
    return changed


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
        return f"{address[:6]}...{address[-4:]}"
    return address


def _build_message(trade: Trade, wallet: Wallet) -> str:
    label = wallet.label or _short_address(wallet.address)
    direction = "BUY" if trade.side == "YES" else "SELL"
    value = trade.price * trade.size
    market = trade.market_title or trade.condition_id
    time_str = trade.traded_at.strftime("%Y-%m-%d %H:%M UTC")
    return (
        f"<b>Trade Alert</b>\n\n"
        f"<b>Wallet:</b> {label}\n"
        f"<b>Market:</b> {market}\n"
        f"<b>Direction:</b> {direction}\n"
        f"<b>Amount:</b> ${value:,.2f}\n"
        f"<b>Time:</b> {time_str}"
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


def fire_alerts_for_new_trades(db: Session, wallet: Wallet) -> int:
    """Send alerts for recent unsent trades that exceed the value threshold.

    Rules enforced on every call:
    - Trades older than 24 h are silently marked alert_sent=1 and skipped.
    - Only trades whose dollar value (price * size) meets the minimum are considered.
    - At most _MAX_ALERTS_PER_WALLET alerts are sent per call (top N by value).
    - Trades beyond that cap are also marked alert_sent=1 so they never re-fire.
    - alert_sent=1 on the Trade row prevents duplicate alerts across refreshes.
    """
    settings = get_app_settings(db)
    if not settings.alerts_enabled:
        logger.debug("fire_alerts: alerts disabled, skipping wallet %s", wallet.address)
        return 0

    token = (settings.telegram_bot_token or "").strip()
    chat_id = (settings.telegram_chat_id or "").strip()
    if not token or not chat_id:
        logger.warning("fire_alerts: Telegram token or chat_id not configured")
        return 0

    # Treat 0 / None as "use default" so misconfigured rows don't spam everything.
    min_value = float(settings.alert_min_size) if settings.alert_min_size else _DEFAULT_MIN_VALUE
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=_LOOKBACK_HOURS)

    # Silently expire stale unsent trades so they never surface again.
    stale_count = (
        db.query(Trade)
        .filter(
            Trade.wallet_address == wallet.address,
            Trade.alert_sent == 0,
            Trade.traded_at < cutoff,
        )
        .update({"alert_sent": 1}, synchronize_session=False)
    )
    if stale_count:
        logger.info("fire_alerts: marked %d stale trades as skipped for wallet %s", stale_count, wallet.address)

    # Fetch all qualifying recent candidates, best value first.
    candidates = (
        db.query(Trade)
        .filter(
            Trade.wallet_address == wallet.address,
            Trade.alert_sent == 0,
            Trade.traded_at >= cutoff,
            (Trade.price * Trade.size) >= min_value,
        )
        .order_by((Trade.price * Trade.size).desc())
        .all()
    )

    to_alert = candidates[:_MAX_ALERTS_PER_WALLET]
    to_skip = candidates[_MAX_ALERTS_PER_WALLET:]

    # Immediately mark over-cap trades so they don't pile up on the next refresh.
    for trade in to_skip:
        trade.alert_sent = 1

    logger.info(
        "fire_alerts: wallet=%s min_value=%.2f candidates=%d sending=%d skipping=%d",
        wallet.address,
        min_value,
        len(candidates),
        len(to_alert),
        len(to_skip),
    )

    sent = 0
    for trade in to_alert:
        text = _build_message(trade, wallet)
        if send_telegram_message(token, chat_id, text):
            trade.alert_sent = 1
            logger.info("fire_alerts: sent alert for trade id=%d value=%.2f", trade.id, trade.price * trade.size)
            sent += 1
        else:
            logger.warning("fire_alerts: failed to send alert for trade id=%d - will retry next refresh", trade.id)

    db.commit()
    return sent
