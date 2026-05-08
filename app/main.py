import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import auth
from app.csrf import csrf_middleware, get_csrf_token
from app.db import SessionLocal, init_db, prune_old_sync_events
from app.routes import router
from app.settings import APP_NAME, DASHBOARD_PASSWORD, LOG_LEVEL, SESSION_SECRET_KEY


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


configure_logging()
logger = logging.getLogger(__name__)

# Paths that do NOT require authentication (login page itself, static assets)
_PUBLIC_PATHS = frozenset({"/login", "/logout"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle - startup and shutdown."""
    logger.info("Initializing database")
    init_db()
    _run_startup_maintenance()
    logger.info("Application startup complete")
    yield


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
    logger.exception("Unhandled application error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


async def security_headers_middleware(request: Request, call_next):
    """Add security headers to every response."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:;"
    )
    return response


async def auth_middleware(request: Request, call_next):
    """Redirect unauthenticated users to /login when auth is configured."""
    path = request.url.path
    if (
        DASHBOARD_PASSWORD
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
    app = FastAPI(title=title or APP_NAME, lifespan=lifespan_context)
    app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET_KEY, same_site="lax", https_only=False)
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


app = create_app()
