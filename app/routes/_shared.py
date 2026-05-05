"""Shared helpers used across all route modules."""
from typing import Any, Dict, Optional, Sequence, Tuple
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Query, Session

from app import view_helpers as vh
from app.models import Wallet

templates = Jinja2Templates(directory="app/templates")
DATE_PRESETS = frozenset({"today", "7d", "30d"})


def _flash_redirect(message: str, level: str = "info") -> RedirectResponse:
    return RedirectResponse(
        url=f"/wallets?flash={quote(message)}&level={quote(level)}", status_code=303
    )


def _flash_redirect_to(url: str, message: str, level: str = "info") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(
        url=f"{url}{sep}flash={quote(message)}&level={quote(level)}", status_code=303
    )


def _safe_next(next_path: Optional[str]) -> Optional[str]:
    if not next_path:
        return None
    if next_path == "/all-trades" or next_path.startswith("/wallets/"):
        return next_path
    return None


def normalized_date_filters(
    date_preset: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    if date_preset in DATE_PRESETS and not date_from and not date_to:
        preset_range = vh.date_preset_range(date_preset)
        return preset_range["date_from"], preset_range["date_to"]
    return date_from, date_to


def paginated_query(query: Query, page: int, page_size: int) -> Tuple[int, int, int, Dict[str, int], Sequence[Any]]:
    total_items = query.count()
    total_pages = max(1, (total_items + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    pagination = vh.pagination_meta(current_page, page_size, total_items)
    items = query.limit(page_size).offset((current_page - 1) * page_size).all()
    return current_page, total_items, total_pages, pagination, items


def _flash_redirect_with_form(
    message: str,
    *,
    level: str = "info",
    address: str = "",
    label: str = "",
    tags: str = "",
    notes: str = "",
) -> RedirectResponse:
    return RedirectResponse(
        url=(
            f"/wallets?flash={quote(message)}&level={quote(level)}&address={quote(address)}"
            f"&label={quote(label)}&tags={quote(tags)}&notes={quote(notes)}"
        ),
        status_code=303,
    )


def resolve_wallet(db: Session, identifier: str) -> Wallet:
    wallet = None
    if identifier.isdigit():
        wallet = db.query(Wallet).filter(Wallet.id == int(identifier)).first()
    if wallet is None:
        wallet = db.query(Wallet).filter(Wallet.address == identifier.strip().lower()).first()
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return wallet
