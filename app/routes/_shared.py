"""Shared helpers used across all route modules."""
from typing import Optional
from urllib.parse import quote

from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.models import Wallet

templates = Jinja2Templates(directory="app/templates")


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
