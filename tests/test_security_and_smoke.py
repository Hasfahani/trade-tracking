"""Security headers audit, deployment smoke tests, and rate limiting contract."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import create_app
from app.models import Base, Trade, Wallet
from datetime import datetime, timezone


def _build_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    app = create_app(lifespan_context=None, csrf_enabled=False)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), testing_session


# --- Security headers ---

REQUIRED_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/wallets"])
def test_security_headers_present_on_every_response(path):
    client, _ = _build_client()
    response = client.get(path)
    for header, expected in REQUIRED_SECURITY_HEADERS.items():
        assert response.headers.get(header) == expected, (
            f"Missing or wrong {header} on {path}: got {response.headers.get(header)!r}"
        )


def test_csp_header_present_and_contains_default_src():
    client, _ = _build_client()
    response = client.get("/healthz")
    csp = response.headers.get("Content-Security-Policy", "")
    assert "default-src" in csp
    assert "frame-ancestors 'none'" in csp


def test_html_responses_have_no_store_cache_control():
    client, _ = _build_client()
    response = client.get("/wallets")
    assert "no-store" in response.headers.get("Cache-Control", "")


def test_server_timing_header_present():
    client, _ = _build_client()
    response = client.get("/healthz")
    assert "Server-Timing" in response.headers
    assert "total;dur=" in response.headers["Server-Timing"]


# --- Deployment smoke tests ---

def test_dashboard_returns_200():
    client, _ = _build_client()
    response = client.get("/dashboard")
    assert response.status_code == 200, f"Dashboard returned {response.status_code}"


def test_all_trades_returns_200():
    client, _ = _build_client()
    response = client.get("/all-trades")
    assert response.status_code == 200, f"All-trades returned {response.status_code}"


def test_wallets_returns_200():
    client, _ = _build_client()
    response = client.get("/wallets")
    assert response.status_code == 200


def test_admin_ops_returns_200_with_expected_keys():
    client, _ = _build_client()
    client.app.state.ready = True
    response = client.get("/admin/ops")
    assert response.status_code == 200
    data = response.json()
    assert "database" in data
    assert "build" in data
    assert "version" in data["build"]
    assert "commit" in data["build"]
    assert "table_counts" in data
    assert "wallets" in data["table_counts"]


def test_admin_schema_version_returns_applied_list():
    client, _ = _build_client()
    response = client.get("/admin/schema-version")
    assert response.status_code == 200
    data = response.json()
    assert "applied" in data
    assert "pending" in data
    assert "total_defined" in data


# --- Rate limiting ---

def test_login_rate_limit_triggers_after_max_attempts(monkeypatch):
    import app.routes.auth as auth_route_mod

    monkeypatch.setattr(auth_route_mod, "DASHBOARD_PASSWORD", "secret123")
    monkeypatch.setattr(auth_route_mod, "_RATE_LIMIT_MAX", 3)
    # Clear attempts between test runs
    auth_route_mod._login_attempts.clear()

    client, _ = _build_client()
    for _ in range(3):
        client.post("/login", data={"password": "wrong"})

    response = client.post("/login", data={"password": "wrong"})
    assert response.status_code == 429


# --- Session secret enforcement ---

def test_weak_session_secret_in_production_with_auth_raises():
    import app.auth as auth_mod
    import app.main as main_mod

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(auth_mod, "DASHBOARD_PASSWORD", "correct-password")
    monkeypatch.setattr(main_mod.app_settings, "IS_PRODUCTION", True)
    monkeypatch.setattr(main_mod.app_settings, "SESSION_SECRET_KEY", main_mod.app_settings.DEFAULT_SESSION_SECRET_KEY)

    with pytest.raises(RuntimeError, match="SESSION_SECRET_KEY is weak"):
        create_app(lifespan_context=None, csrf_enabled=False)

    monkeypatch.undo()


def test_production_without_dashboard_password_raises(monkeypatch):
    import app.auth as auth_mod
    import app.main as main_mod

    monkeypatch.setattr(auth_mod, "DASHBOARD_PASSWORD", None)
    monkeypatch.setattr(main_mod.app_settings, "IS_PRODUCTION", True)
    monkeypatch.setattr(main_mod.app_settings, "SESSION_SECRET_KEY", "x" * 40)

    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD must be set"):
        create_app(lifespan_context=None, csrf_enabled=False)


def test_strong_session_secret_in_production_starts_without_error(monkeypatch):
    import app.auth as auth_mod
    import app.main as main_mod

    monkeypatch.setattr(auth_mod, "DASHBOARD_PASSWORD", "correct-password")
    monkeypatch.setattr(main_mod.app_settings, "IS_PRODUCTION", True)
    monkeypatch.setattr(main_mod.app_settings, "SESSION_SECRET_KEY", "x" * 40)

    app = create_app(lifespan_context=None, csrf_enabled=False)
    assert app is not None
