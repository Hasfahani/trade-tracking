# Limits repeated requests from the same user.
"""Shared rate-limiter instance, imported by main.py and route modules."""

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request


def get_client_ip(request: Request) -> str:
    """Return a stable client IP when running behind hosted-platform proxies."""
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()

    for header in ("cf-connecting-ip", "x-real-ip"):
        value = request.headers.get(header, "").strip()
        if value:
            return value

    return get_remote_address(request)


limiter = Limiter(key_func=get_client_ip)
