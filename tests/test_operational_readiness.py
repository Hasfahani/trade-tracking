# Tests app health and readiness.
from fastapi.testclient import TestClient
import time

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


def test_pages_show_503_when_startup_has_not_marked_ready():
    import app.main as main_mod

    async def no_ready_startup():
        return None

    app = create_app(lifespan_context=None, csrf_enabled=False)
    app.state.ready = False
    app.state.startup_task = None

    @app.on_event("startup")
    async def _start_unready_task():
        app.state.startup_task = main_mod.asyncio.create_task(no_ready_startup())

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/wallets", headers={"accept": "text/html"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "3"
    assert "Still starting" in response.text


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
    monkeypatch.setattr(main_mod, "RETENTION_METRICS_ENABLED", False)

    app = create_app(lifespan_context=lifespan, csrf_enabled=False)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/readyz")
        deadline = time.monotonic() + 2
        while response.status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.01)
            response = client.get("/readyz")

    assert attempts["count"] == 2
    assert response.status_code == 200


def test_startup_maintenance_seeds_wallets_when_enabled(monkeypatch):
    import app.main as main_mod

    called = {"seed": False, "prune": False, "commit": False, "rollback": False, "close": False}

    class FakeDb:
        def commit(self):
            called["commit"] = True

        def rollback(self):
            called["rollback"] = True

        def close(self):
            called["close"] = True

    def fake_seed(db):
        called["seed"] = True
        return {"added": 8, "updated": 0, "total": 8}

    def fake_prune(db, keep_days):
        called["prune"] = True
        return 0

    monkeypatch.setattr(main_mod.app_settings, "STARTUP_SEED_WALLETS", True)
    monkeypatch.setattr(main_mod, "SessionLocal", lambda: FakeDb())
    monkeypatch.setattr(main_mod, "prune_old_sync_events", fake_prune)
    monkeypatch.setattr("app.watchlist_seed.seed_watchlist_wallets", fake_seed)

    main_mod._run_startup_maintenance()

    assert called == {"seed": True, "prune": True, "commit": True, "rollback": False, "close": True}


def test_startup_degrades_instead_of_crashing_after_database_failures(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod.app_settings, "STARTUP_DB_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(main_mod.app_settings, "STARTUP_DB_RETRY_SECONDS", 0)
    monkeypatch.setattr(main_mod, "init_db", lambda: (_ for _ in ()).throw(RuntimeError("database down")))
    monkeypatch.setattr(main_mod, "RETENTION_METRICS_ENABLED", False)

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


def test_public_base_url_drives_robots_and_sitemap(monkeypatch):
    import app.routes.core as core_mod

    monkeypatch.setattr(core_mod, "PUBLIC_BASE_URL", "https://polysignal.onrender.com")
    app = create_app(lifespan_context=None, csrf_enabled=False)
    client = TestClient(app, raise_server_exceptions=False)

    robots = client.get("/robots.txt")
    sitemap = client.get("/sitemap.xml")

    assert robots.status_code == 200
    assert "Sitemap: https://polysignal.onrender.com/sitemap.xml" in robots.text
    assert "<loc>https://polysignal.onrender.com/wallets</loc>" in sitemap.text


def test_auth_enabled_protected_route_redirects_without_session_error(monkeypatch):
    import app.auth as auth_mod
    import app.routes.auth as auth_route_mod

    monkeypatch.setattr(auth_mod, "DASHBOARD_PASSWORD", "correct-password")
    monkeypatch.setattr(auth_route_mod, "DASHBOARD_PASSWORD", "correct-password")
    app = create_app(lifespan_context=None, csrf_enabled=False)
    app.state.ready = True
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/wallets", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/login?next=/wallets"


def test_public_read_only_allows_analysis_but_keeps_admin_private(monkeypatch):
    import app.auth as auth_mod
    import app.routes.auth as auth_route_mod
    import app.main as main_mod

    monkeypatch.setattr(auth_mod, "DASHBOARD_PASSWORD", "correct-password")
    monkeypatch.setattr(auth_route_mod, "DASHBOARD_PASSWORD", "correct-password")
    monkeypatch.setattr(main_mod.app_settings, "PUBLIC_READ_ONLY", True)
    app = create_app(lifespan_context=None, csrf_enabled=False)
    app.state.ready = True
    client = TestClient(app, raise_server_exceptions=False)

    public_page = client.get("/all-trades", follow_redirects=False)
    assert public_page.status_code == 200

    public_analysis = client.get("/api/trades/missing/ai-analysis", follow_redirects=False)
    assert public_analysis.status_code == 404

    protected_admin = client.get("/admin/train-model", follow_redirects=False)
    assert protected_admin.status_code == 302
    assert protected_admin.headers["location"] == "/login?next=/admin/train-model"

    protected_mutation = client.post(
        "/api/trades/missing/ai-analysis/invalidate",
        follow_redirects=False,
    )
    assert protected_mutation.status_code == 302


def test_auto_refresh_scheduler_disabled_with_multiple_workers(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod.app_settings, "AUTO_REFRESH_INTERVAL_MINUTES", 30)
    monkeypatch.setattr(main_mod.app_settings, "WEB_CONCURRENCY", 2)
    monkeypatch.setattr(main_mod.app_settings, "AUTO_REFRESH_ALLOW_MULTI_WORKER", False)

    assert main_mod._should_start_auto_refresh_scheduler() is False


def test_auto_refresh_job_skips_until_app_ready(monkeypatch):
    import app.main as main_mod

    called = {"value": False}

    def fake_refresh():
        called["value"] = True

    monkeypatch.setattr(main_mod, "_auto_refresh_wallets", fake_refresh)
    app = create_app(lifespan_context=None, csrf_enabled=False)
    app.state.ready = False

    main_mod.asyncio.run(main_mod._run_auto_refresh_job(app))

    assert called["value"] is False


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
