from fastapi.testclient import TestClient

from app.main import create_app, lifespan


def test_healthz_is_always_200_and_exposes_readiness_state():
    app = create_app(lifespan_context=None, csrf_enabled=False)
    app.state.ready = False
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["ready"] is False


def test_readyz_returns_503_when_startup_is_degraded():
    app = create_app(lifespan_context=None, csrf_enabled=False)
    app.state.ready = False
    app.state.startup_error = "database initialization failed"
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": "unavailable",
        "startup_error": "database initialization failed",
    }


def test_readyz_returns_200_after_database_check(monkeypatch):
    import app.routes.core as core_mod

    monkeypatch.setattr(core_mod, "check_database_ready", lambda: None)
    app = create_app(lifespan_context=None, csrf_enabled=False)
    app.state.ready = True
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["database"] == "ok"


def test_startup_retries_transient_database_failures(monkeypatch):
    import app.main as main_mod
    import app.routes.core as core_mod

    attempts = {"count": 0}

    def flaky_init_db():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("temporary database failure")

    monkeypatch.setattr(main_mod.app_settings, "STARTUP_DB_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(main_mod.app_settings, "STARTUP_DB_RETRY_SECONDS", 0)
    monkeypatch.setattr(main_mod, "init_db", flaky_init_db)
    monkeypatch.setattr(main_mod, "check_database_ready", lambda: None)
    monkeypatch.setattr(core_mod, "check_database_ready", lambda: None)
    monkeypatch.setattr(main_mod, "_run_startup_maintenance", lambda: None)

    app = create_app(lifespan_context=lifespan, csrf_enabled=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/readyz")

    assert attempts["count"] == 2
    assert response.status_code == 200


def test_startup_degrades_instead_of_crashing_after_database_failures(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod.app_settings, "STARTUP_DB_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(main_mod.app_settings, "STARTUP_DB_RETRY_SECONDS", 0)
    monkeypatch.setattr(main_mod, "init_db", lambda: (_ for _ in ()).throw(RuntimeError("database down")))

    app = create_app(lifespan_context=lifespan, csrf_enabled=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        health = client.get("/healthz")
        ready = client.get("/readyz")

    assert health.status_code == 200
    assert ready.status_code == 503
    assert ready.json()["startup_error"] == "database initialization failed"


def test_request_id_is_returned_and_preserved_from_header():
    app = create_app(lifespan_context=None, csrf_enabled=False)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/healthz", headers={"X-Request-ID": "test-request-123"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "test-request-123"


def test_production_auth_with_weak_session_secret_raises():
    import app.auth as auth_mod
    import app.main as main_mod
    import pytest

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(auth_mod, "DASHBOARD_PASSWORD", "correct-password")
    monkeypatch.setattr(main_mod.app_settings, "IS_PRODUCTION", True)
    monkeypatch.setattr(main_mod.app_settings, "SESSION_SECRET_KEY", main_mod.app_settings.DEFAULT_SESSION_SECRET_KEY)

    with pytest.raises(RuntimeError, match="SESSION_SECRET_KEY is weak"):
        create_app(lifespan_context=None, csrf_enabled=False)

    monkeypatch.undo()


def test_secure_session_cookie_can_be_enabled_for_reverse_proxy_https(monkeypatch):
    import app.auth as auth_mod
    import app.routes.auth as auth_route_mod
    import app.main as main_mod

    monkeypatch.setattr(auth_mod, "DASHBOARD_PASSWORD", "correct-password")
    monkeypatch.setattr(auth_route_mod, "DASHBOARD_PASSWORD", "correct-password")
    monkeypatch.setattr(main_mod.app_settings, "SESSION_COOKIE_SECURE", True)
    monkeypatch.setattr(main_mod.app_settings, "SESSION_SECRET_KEY", "x" * 40)

    app = create_app(lifespan_context=None, csrf_enabled=False)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post("/login", data={"password": "correct-password"}, follow_redirects=False)

    assert response.status_code == 303
    assert "secure" in response.headers["set-cookie"].lower()
    assert "httponly" in response.headers["set-cookie"].lower()
