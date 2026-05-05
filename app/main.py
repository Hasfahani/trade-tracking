import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.routes import router
from app.settings import APP_NAME, LOG_LEVEL


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle - startup and shutdown."""
    logger.info("Initializing database")
    init_db()
    logger.info("Application startup complete")
    yield


async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled application error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


def create_app(*, lifespan_context=lifespan, title: Optional[str] = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title=title or APP_NAME, lifespan=lifespan_context)
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(router)
    app.add_exception_handler(Exception, unhandled_exception_handler)
    return app


app = create_app()
