# Summary: Tests request limiting.
# Details: It checks this part of the project so future code changes do not silently break expected behavior.
from starlette.requests import Request

from app.limiter import get_client_ip


def _request(headers=None):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in (headers or {}).items()
            ],
            "client": ("10.0.0.10", 12345),
        }
    )


def test_rate_limit_key_uses_first_forwarded_for_ip():
    request = _request({"X-Forwarded-For": "203.0.113.8, 10.0.0.1"})

    assert get_client_ip(request) == "203.0.113.8"


def test_rate_limit_key_falls_back_to_socket_ip():
    request = _request()

    assert get_client_ip(request) == "10.0.0.10"
