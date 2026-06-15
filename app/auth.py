# Handles login and session checks.
"""Session-based authentication helpers.

When DASHBOARD_PASSWORD is set the app requires a login before any route.
When unset the app runs unauthenticated (preserves local-use behaviour).
"""
import logging
from typing import Optional

from fastapi import Request
from fastapi.responses import RedirectResponse

from app.settings import DASHBOARD_PASSWORD

logger = logging.getLogger(__name__)

_SESSION_KEY = "authenticated"
_LOGIN_PATH = "/login"


def auth_enabled() -> bool:
    """Return True when password-based auth is configured."""
    return bool(DASHBOARD_PASSWORD)


def is_authenticated(request: Request) -> bool:
    """Return True when the request session is marked as authenticated."""
    if not auth_enabled():
        return True
    return bool(request.session.get(_SESSION_KEY))


def mark_authenticated(request: Request) -> None:
    """Stamp the session as authenticated after a successful login."""
    request.session[_SESSION_KEY] = True


def clear_session(request: Request) -> None:
    """Remove the authentication stamp from the session."""
    request.session.pop(_SESSION_KEY, None)


def require_auth(request: Request) -> Optional[RedirectResponse]:
    """Return a redirect to /login if authentication is required but missing.

    Use this at the top of every route handler that should be protected:

        redir = require_auth(request)
        if redir:
            return redir
    """
    if not is_authenticated(request):
        next_url = str(request.url.path)
        return RedirectResponse(url=f"{_LOGIN_PATH}?next={next_url}", status_code=302)
    return None
