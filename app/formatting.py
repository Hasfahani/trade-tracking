from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.models import Wallet

WALLET_STALE_HOURS = 24


def short_address(address: str) -> str:
    if len(address) <= 14:
        return address
    return f"{address[:8]}...{address[-6:]}"


def duration_label(duration_ms: Optional[int]) -> str:
    if duration_ms is None:
        return "-"
    if duration_ms < 1000:
        return f"{duration_ms} ms"
    return f"{duration_ms / 1000:.1f} s"


def wallet_status_tone(wallet: Wallet) -> str:
    if wallet.last_refresh_status == "error":
        return "danger"
    if not wallet.last_checked_at:
        return "warning"
    now = datetime.now(timezone.utc)
    checked_at = wallet.last_checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    age = now - checked_at
    if age <= timedelta(hours=WALLET_STALE_HOURS):
        return "success"
    return "warning"


def wallet_freshness_label(wallet: Wallet) -> str:
    if wallet.last_refresh_status == "error":
        return "Failed"
    if not wallet.last_checked_at:
        return "Never refreshed"
    now = datetime.now(timezone.utc)
    checked_at = wallet.last_checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    age = now - checked_at
    if age <= timedelta(hours=WALLET_STALE_HOURS):
        return "Fresh"
    return "Stale"


def sync_status_class(status: Optional[str]) -> str:
    if status == "error":
        return "danger"
    if status == "no_new":
        return "warning"
    if status == "success":
        return "success"
    return "info"


def date_preset_range(preset: Optional[str]) -> Dict[str, Optional[str]]:
    today = datetime.now().date()
    if preset == "today":
        value = today.isoformat()
        return {"date_from": value, "date_to": value}
    if preset == "7d":
        return {"date_from": (today - timedelta(days=6)).isoformat(), "date_to": today.isoformat()}
    if preset == "30d":
        return {"date_from": (today - timedelta(days=29)).isoformat(), "date_to": today.isoformat()}
    return {"date_from": None, "date_to": None}


def parse_datetime_start(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def parse_datetime_end(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text) + timedelta(days=1)
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def pagination_meta(page: int, page_size: int, total_items: int) -> Dict[str, int]:
    if total_items <= 0:
        return {"start": 0, "end": 0}
    start = ((page - 1) * page_size) + 1
    end = min(total_items, page * page_size)
    return {"start": start, "end": end}


def active_wallets(wallets: List[Wallet]) -> List[Wallet]:
    return [w for w in wallets if not w.is_archived]
