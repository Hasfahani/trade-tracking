"""Login and logout routes."""
import time
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth
from app.settings import APP_NAME, DASHBOARD_PASSWORD

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

# Simple in-memory rate limit: max 10 failed attempts per IP per 15 minutes
_login_attempts: dict = defaultdict(list)
_RATE_LIMIT_WINDOW = 900  # 15 minutes
_RATE_LIMIT_MAX = 10


def _is_rate_limited(ip: str) -> bool:
    now = time.monotonic()
    window_start = now - _RATE_LIMIT_WINDOW
    attempts = [t for t in _login_attempts[ip] if t > window_start]
    _login_attempts[ip] = attempts
    return len(attempts) >= _RATE_LIMIT_MAX


def _record_failed_attempt(ip: str) -> None:
    _login_attempts[ip].append(time.monotonic())

_SAFE_NEXT_PREFIXES = ("/wallets", "/dashboard", "/all-trades", "/trades", "/admin", "/settings")


def _safe_next(next_path: Optional[str]) -> str:
    if next_path and any(next_path.startswith(p) for p in _SAFE_NEXT_PREFIXES):
        return next_path
    return "/wallets"


@router.get("/login")
async def login_page(request: Request, next: Optional[str] = None):
    if auth.is_authenticated(request):
        return RedirectResponse(url=_safe_next(next), status_code=302)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"request": request, "app_name": APP_NAME, "error": None, "next": next or ""},
    )


@router.post("/login")
async def login_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form(""),
):
    client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown").split(",")[0].strip()
    if _is_rate_limited(client_ip):
        return JSONResponse(
            {"detail": "Too many login attempts. Please wait 15 minutes before trying again."},
            status_code=429,
        )
    if DASHBOARD_PASSWORD and password == DASHBOARD_PASSWORD:
        auth.mark_authenticated(request)
        return RedirectResponse(url=_safe_next(next or None), status_code=303)
    _record_failed_attempt(client_ip)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "error": "Incorrect password.",
            "next": next,
        },
        status_code=401,
    )


@router.get("/logout")
async def logout(request: Request):
    auth.clear_session(request)
    return RedirectResponse(url="/login", status_code=302)
