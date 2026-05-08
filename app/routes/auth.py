"""Login and logout routes."""
from typing import Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from app import auth
from app.settings import APP_NAME, DASHBOARD_PASSWORD

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

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
    if DASHBOARD_PASSWORD and password == DASHBOARD_PASSWORD:
        auth.mark_authenticated(request)
        return RedirectResponse(url=_safe_next(next or None), status_code=303)
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
