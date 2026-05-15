"""Simple double-submit cookie CSRF protection.

A random token is placed in a cookie and also injected into every HTML form as
a hidden field (via JavaScript in base_v2.html).  On POST the middleware
checks that the submitted form field matches the cookie value.

Exempt paths: /login (no session exists yet; the password is the credential).

After reading the form body to extract the CSRF token, we rely on Starlette's
request body caching so downstream Form(...) dependencies can still read the
same payload safely.
"""
import logging
import secrets
from typing import Callable
from urllib.parse import parse_qs

from fastapi import Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

from app import settings as app_settings

logger = logging.getLogger(__name__)

_COOKIE_NAME = "csrftoken"
_FORM_FIELD = "csrf_token"
_EXEMPT_PATHS = frozenset({"/login"})
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_COOKIE_MAX_AGE = 60 * 60 * 8  # 8 hours


def _get_or_create_token(request: Request) -> str:
    token = request.cookies.get(_COOKIE_NAME)
    if not token or len(token) < 32:
        token = secrets.token_hex(32)
    return token


def _csrf_cookie_secure(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return app_settings.CSRF_COOKIE_SECURE or forwarded_proto == "https" or request.url.scheme == "https"


async def csrf_middleware(request: Request, call_next: Callable) -> Response:
    """Validate CSRF token on mutating requests; set cookie on every response."""
    token = _get_or_create_token(request)

    if request.method in _MUTATING_METHODS and request.url.path not in _EXEMPT_PATHS:
        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            body = await request.body()

            # Parse the CSRF field from the raw URL-encoded form body.
            # multipart forms are not validated here (they're not used by this app).
            params = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
            submitted = (params.get(_FORM_FIELD) or [""])[0]

            if submitted != token:
                logger.warning("CSRF token mismatch on %s %s", request.method, request.url.path)
                try:
                    _tmpl = Jinja2Templates(directory="app/templates")
                    return _tmpl.TemplateResponse(
                        request,
                        "403.html",
                        {"request": request, "app_name": app_settings.APP_NAME},
                        status_code=403,
                    )
                except Exception:
                    return HTMLResponse(
                        "<h1>403 Forbidden</h1><p>CSRF token invalid or missing. Please go back and try again.</p>",
                        status_code=403,
                    )

    response = await call_next(request)
    response.set_cookie(
        _COOKIE_NAME,
        token,
        max_age=_COOKIE_MAX_AGE,
        httponly=False,
        samesite="lax",
        secure=_csrf_cookie_secure(request),
    )
    return response


def get_csrf_token(request: Request) -> str:
    """Return the current CSRF token for this request (for template injection)."""
    return _get_or_create_token(request)
