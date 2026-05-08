"""Pure formatting helpers with no database dependencies.

All functions here are side-effect-free and safe to call from templates or
anywhere in the application.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.models import Wallet

WALLET_STALE_HOURS = 24


def short_address(address: str) -> str:
    """Return a shortened wallet address for display.

    Args:
        address: Full 0x-prefixed hex address.

    Returns:
        Original address if ≤14 chars, otherwise ``0x123456...abcdef`` form.
    """
    if len(address) <= 14:
        return address
    return f"{address[:8]}...{address[-6:]}"


def duration_label(duration_ms: Optional[int]) -> str:
    """Format a millisecond duration as a human-readable string.

    Args:
        duration_ms: Duration in milliseconds, or ``None``.

    Returns:
        ``"-"`` when ``None``; ``"NNN ms"`` for sub-second; ``"N.N s"`` otherwise.
    """
    if duration_ms is None:
        return "-"
    if duration_ms < 1000:
        return f"{duration_ms} ms"
    return f"{duration_ms / 1000:.1f} s"


def wallet_status_tone(wallet: Wallet) -> str:
    """Return a CSS tone class for a wallet's refresh status.

    Args:
        wallet: The Wallet ORM instance to inspect.

    Returns:
        One of ``"danger"``, ``"warning"``, or ``"success"``.
    """
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
    """Return a human-readable freshness label for a wallet.

    Args:
        wallet: The Wallet ORM instance to inspect.

    Returns:
        One of ``"Failed"``, ``"Never refreshed"``, ``"Fresh"``, or ``"Stale"``.
    """
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
    """Map a SyncEvent status string to a CSS tone class.

    Args:
        status: Raw status string from the database (e.g. ``"success"``).

    Returns:
        One of ``"danger"``, ``"warning"``, ``"success"``, or ``"info"``.
    """
    if status == "error":
        return "danger"
    if status == "no_new":
        return "warning"
    if status == "success":
        return "success"
    return "info"


def date_preset_range(preset: Optional[str]) -> Dict[str, Optional[str]]:
    """Convert a named date preset to an ISO date range dict.

    Args:
        preset: One of ``"today"``, ``"7d"``, ``"30d"``, or any other value.

    Returns:
        Dict with ``"date_from"`` and ``"date_to"`` keys (ISO strings or ``None``).
    """
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
    """Parse a date string as the inclusive start of a range.

    Args:
        value: ISO date string (e.g. ``"2026-01-15"``), or ``None``.

    Returns:
        Parsed ``datetime`` at midnight, or ``None`` on failure.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def parse_datetime_end(value: Optional[str]) -> Optional[datetime]:
    """Parse a date string as the exclusive end of a range.

    A bare date (``YYYY-MM-DD``) is advanced by one day so the range is inclusive
    of the named day.

    Args:
        value: ISO date string, or ``None``.

    Returns:
        Parsed ``datetime``, or ``None`` on failure.
    """
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
    """Return start/end display indices for pagination UI.

    Args:
        page: Current 1-based page number.
        page_size: Items per page.
        total_items: Total number of items across all pages.

    Returns:
        Dict with ``"start"`` and ``"end"`` (both inclusive, 1-based), or
        ``{"start": 0, "end": 0}`` when ``total_items`` is zero.
    """
    if total_items <= 0:
        return {"start": 0, "end": 0}
    start = ((page - 1) * page_size) + 1
    end = min(total_items, page * page_size)
    return {"start": start, "end": end}


def active_wallets(wallets: List[Wallet]) -> List[Wallet]:
    """Filter out archived wallets from a list.

    Args:
        wallets: Iterable of Wallet ORM instances.

    Returns:
        List containing only wallets where ``is_archived`` is falsy.
    """
    return [w for w in wallets if not w.is_archived]
