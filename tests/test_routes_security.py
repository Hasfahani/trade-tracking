# Summary: Tests login and form security.
# Details: It checks this part of the project so future code changes do not silently break expected behavior.
"""Tests for authentication and CSRF protection."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import create_app
from app.models import Base


def _make_client(dashboard_password=None, csrf_enabled=True):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    import app.settings as settings_mod
    original = settings_mod.DASHBOARD_PASSWORD
    settings_mod.DASHBOARD_PASSWORD = dashboard_password

    import app.main as main_mod
    original_main = main_mod.DASHBOARD_PASSWORD
    main_mod.DASHBOARD_PASSWORD = dashboard_password

    import app.auth as auth_mod
    original_auth = auth_mod.DASHBOARD_PASSWORD
    auth_mod.DASHBOARD_PASSWORD = dashboard_password

    app = create_app(lifespan_context=None, csrf_enabled=csrf_enabled)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, raise_server_exceptions=False)
    return client


class TestAuthDisabled:
    def test_unauthenticated_access_allowed_when_no_password(self):
        client = _make_client(dashboard_password=None, csrf_enabled=False)
        resp = client.get("/wallets", follow_redirects=False)
        assert resp.status_code == 200

    def test_login_route_exists_even_without_password(self):
        client = _make_client(dashboard_password=None, csrf_enabled=False)
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_logout_redirects_to_login(self):
        client = _make_client(dashboard_password=None, csrf_enabled=False)
        resp = client.get("/logout", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["location"]


class TestSecurityHeaders:
    def test_security_headers_present_on_every_response(self):
        client = _make_client(dashboard_password=None, csrf_enabled=False)
        resp = client.get("/wallets")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "same-origin"
        assert "Content-Security-Policy" in resp.headers

    def test_csp_header_restricts_to_self(self):
        client = _make_client(dashboard_password=None, csrf_enabled=False)
        resp = client.get("/wallets")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp


class TestCSRFProtection:
    def test_post_without_csrf_token_returns_403(self):
        client = _make_client(dashboard_password=None, csrf_enabled=True)
        # First GET to get the cookie
        client.get("/wallets")
        # POST without csrf_token field
        resp = client.post("/wallets", data={"address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"})
        assert resp.status_code == 403

    def test_post_with_valid_csrf_token_succeeds(self):
        client = _make_client(dashboard_password=None, csrf_enabled=True)
        get_resp = client.get("/wallets")
        token = client.cookies.get("csrftoken")
        assert token is not None
        resp = client.post(
            "/wallets",
            data={
                "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_post_with_wrong_csrf_token_returns_403(self):
        client = _make_client(dashboard_password=None, csrf_enabled=True)
        client.get("/wallets")
        resp = client.post(
            "/wallets",
            data={"address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "csrf_token": "bad-token"},
        )
        assert resp.status_code == 403


class TestLoginLogout:
    def test_login_route_accessible(self):
        # When auth is disabled, /login redirects to /wallets (user is always authenticated).
        client = _make_client(dashboard_password=None, csrf_enabled=False)
        resp = client.get("/login", follow_redirects=False)
        assert resp.status_code in (200, 302)

    def test_login_post_wrong_password_returns_401(self):
        # When no password is set, any submission returns 401 (no correct password exists).
        client = _make_client(dashboard_password=None, csrf_enabled=False)
        resp = client.post("/login", data={"password": "wrong", "next": ""}, follow_redirects=False)
        assert resp.status_code == 401
