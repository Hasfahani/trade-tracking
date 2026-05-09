import contextvars
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import auth
from app import settings as app_settings
from app.csrf import csrf_middleware, get_csrf_token
from app.db import SessionLocal, check_database_ready, init_db, prune_old_sync_events
from app.routes import router
from app.settings import APP_NAME, DASHBOARD_PASSWORD, LOG_LEVEL, SESSION_SECRET_KEY


_request_id_context: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestIdFilter(logging.Filter):
    """Attach the current request id to records that do not already have one."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = _request_id_context.get()
        return True


def configure_logging() -> None:
    logging.setLogRecordFactory(logging.LogRecord)
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] [request_id=%(request_id)s] %(message)s",
    )
    request_id_filter = RequestIdFilter()
    for handler in logging.getLogger().handlers:
        if not any(isinstance(existing_filter, RequestIdFilter) for existing_filter in handler.filters):
            handler.addFilter(request_id_filter)


configure_logging()
logger = logging.getLogger(__name__)

# Paths that do NOT require authentication (login page itself, static assets)
_PUBLIC_PATHS = frozenset({"/login", "/logout", "/healthz", "/readyz"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle - startup and shutdown."""
    app.state.ready = False
    app.state.startup_error = None
    logger.info(
        "Application startup beginning env=%s production=%s database=%s auth_enabled=%s",
        app_settings.APP_ENV,
        app_settings.IS_PRODUCTION,
        _database_label(),
        auth.auth_enabled(),
    )
    if _initialize_database_with_retries():
        _run_startup_maintenance()
        app.state.ready = True
        logger.info("Application startup complete")
    else:
        app.state.startup_error = "database initialization failed"
        logger.error("Application startup degraded: database initialization failed")
    yield


def _database_label() -> str:
    """Return a non-secret database label for logs and health payloads."""
    if app_settings.DATABASE_URL.startswith("sqlite"):
        return "sqlite"
    if app_settings.DATABASE_URL.startswith("postgresql"):
        return "postgresql"
    return "configured"


def _initialize_database_with_retries() -> bool:
    attempts = max(1, app_settings.STARTUP_DB_MAX_ATTEMPTS)
    delay = max(0.0, app_settings.STARTUP_DB_RETRY_SECONDS)
    for attempt in range(1, attempts + 1):
        try:
            logger.info("Initializing database attempt=%d/%d", attempt, attempts)
            init_db()
            check_database_ready()
            return True
        except Exception as exc:
            logger.exception("Database initialization attempt %d/%d failed: %s", attempt, attempts, exc.__class__.__name__)
            if attempt < attempts and delay:
                time.sleep(delay)
    return False


def _run_startup_maintenance() -> None:
    """Run lightweight once-per-startup housekeeping tasks."""
    try:
        db = SessionLocal()
        removed = prune_old_sync_events(db, keep_days=90)
        if removed:
            logger.info("Startup maintenance: pruned %d old sync events", removed)
        db.close()
    except Exception:
        logger.exception("Startup maintenance failed — continuing anyway")


async def unhandled_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "-")
    request_id_token = _request_id_context.set(request_id)
    try:
        logger.exception(
            "Unhandled application error method=%s path=%s",
            request.method,
            request.url.path,
        )
    finally:
        _request_id_context.reset(request_id_token)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id},
        headers={"X-Request-ID": request_id},
    )


async def request_logging_middleware(request: Request, call_next):
    """Log every request with a stable request id and duration."""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = request_id
    request_id_token = _request_id_context.set(request_id)
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = int((time.perf_counter() - started) * 1000)
        logger.exception(
            "Request failed method=%s path=%s duration_ms=%d",
            request.method,
            request.url.path,
            duration_ms,
        )
        raise
    else:
        duration_ms = int((time.perf_counter() - started) * 1000)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "Request complete method=%s path=%s status=%d duration_ms=%d",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response
    finally:
        _request_id_context.reset(request_id_token)


async def security_headers_middleware(request: Request, call_next):
    """Add security headers to every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "base-uri 'self'; "
        "frame-ancestors 'none';"
    )
    return response


async def auth_middleware(request: Request, call_next):
    """Redirect unauthenticated users to /login when auth is configured."""
    path = request.url.path
    if (
        auth.auth_enabled()
        and path not in _PUBLIC_PATHS
        and not path.startswith("/static")
        and not auth.is_authenticated(request)
    ):
        return RedirectResponse(url=f"/login?next={path}", status_code=302)
    return await call_next(request)


def create_app(
    *,
    lifespan_context=lifespan,
    title: Optional[str] = None,
    csrf_enabled: bool = True,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    _validate_runtime_configuration()
    app = FastAPI(title=title or APP_NAME, lifespan=lifespan_context)
    app.state.ready = False
    app.state.startup_error = None
    app.add_middleware(
        SessionMiddleware,
        secret_key=SESSION_SECRET_KEY,
        session_cookie=app_settings.SESSION_COOKIE_NAME,
        same_site="lax",
        https_only=app_settings.SESSION_COOKIE_SECURE,
        max_age=60 * 60 * 8,
    )
    app.middleware("http")(request_logging_middleware)
    app.middleware("http")(security_headers_middleware)
    app.middleware("http")(auth_middleware)
    if csrf_enabled:
        app.middleware("http")(csrf_middleware)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(router)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    # Inject csrf_token helper into every Jinja2 template context
    from app.routes._shared import templates as _templates
    _templates.env.globals["get_csrf_token"] = get_csrf_token

    return app


def _validate_runtime_configuration() -> None:
    if app_settings.session_secret_is_weak():
        message = "SESSION_SECRET_KEY is weak or using the default value"
        if app_settings.IS_PRODUCTION and auth.auth_enabled():
            logger.error("%s; set a random 32+ character secret for stable, safe production sessions", message)
            return
        logger.warning("%s; sessions are not production-hardened", message)
    if app_settings.IS_PRODUCTION and not auth.auth_enabled():
        logger.warning("DASHBOARD_PASSWORD is not set; production deployment will be publicly accessible")


app = create_app()
