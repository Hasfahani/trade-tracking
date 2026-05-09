import os
import subprocess
from pathlib import Path
from typing import Optional

# Base directory
BASE_DIR = Path(__file__).resolve().parent.parent


def _read_git_commit() -> str:
    """Return the current git commit SHA, preferring the env var set at build/deploy time."""
    commit = os.getenv("GIT_COMMIT", "").strip()
    if commit:
        return commit
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Database
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
_raw_db_url = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'app.db'}")
# Normalise PostgreSQL URLs:
# - Railway/Heroku emit postgres:// which SQLAlchemy 2 doesn't recognise
# - Plain postgresql:// uses psycopg2 by default; explicitly route to psycopg (v3)
if _raw_db_url.startswith("postgres://"):
    _raw_db_url = _raw_db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif _raw_db_url.startswith("postgresql://"):
    _raw_db_url = _raw_db_url.replace("postgresql://", "postgresql+psycopg://", 1)
DATABASE_URL = _raw_db_url

# App metadata
APP_NAME = _env_str("APP_NAME", "PolySignal")
APP_VERSION = _env_str("APP_VERSION", "0.0.0")
GIT_COMMIT = _read_git_commit()
LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()
APP_ENV = _env_str("APP_ENV", os.getenv("RAILWAY_ENVIRONMENT", "development")).lower()
IS_PRODUCTION = APP_ENV in {"production", "prod"} or bool(os.getenv("RAILWAY_ENVIRONMENT"))

# Server runtime
HOST = os.getenv("HOST", "0.0.0.0")
PORT = _env_int("PORT", 8000)

# Pagination defaults
DEFAULT_PAGE_SIZE = _env_int("DEFAULT_PAGE_SIZE", 50)
MAX_PAGE_SIZE = _env_int("MAX_PAGE_SIZE", 200)

# Ingestion behavior
DEFAULT_REFRESH_LIMIT = _env_int("DEFAULT_REFRESH_LIMIT", 200)
POLYMARKET_CONNECT_TIMEOUT_SECONDS = _env_float("POLYMARKET_CONNECT_TIMEOUT_SECONDS", 5.0)
POLYMARKET_READ_TIMEOUT_SECONDS = _env_float("POLYMARKET_READ_TIMEOUT_SECONDS", 15.0)
POLYMARKET_WRITE_TIMEOUT_SECONDS = _env_float("POLYMARKET_WRITE_TIMEOUT_SECONDS", 15.0)
POLYMARKET_POOL_TIMEOUT_SECONDS = _env_float("POLYMARKET_POOL_TIMEOUT_SECONDS", 5.0)

# Authentication — if set, all routes require a session login
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip() or None

# Session secret key for itsdangerous / Starlette sessions
DEFAULT_SESSION_SECRET_KEY = "change-me-in-production-use-a-long-random-secret"
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", DEFAULT_SESSION_SECRET_KEY)

# Cookie/security behavior. Railway terminates TLS before forwarding to the app,
# so production defaults should emit Secure cookies even though uvicorn sees HTTP.
SESSION_COOKIE_NAME = _env_str("SESSION_COOKIE_NAME", "polysignal_session")
SESSION_COOKIE_SECURE = _env_str(
    "SESSION_COOKIE_SECURE",
    "true" if IS_PRODUCTION else "false",
).lower() in {"1", "true", "yes", "on"}
CSRF_COOKIE_SECURE = _env_str(
    "CSRF_COOKIE_SECURE",
    "true" if IS_PRODUCTION else "false",
).lower() in {"1", "true", "yes", "on"}

# Startup/readiness tuning
STARTUP_DB_MAX_ATTEMPTS = _env_int("STARTUP_DB_MAX_ATTEMPTS", 3)
STARTUP_DB_RETRY_SECONDS = _env_float("STARTUP_DB_RETRY_SECONDS", 1.0)

# Connection pool tuning (PostgreSQL only; SQLite uses StaticPool/NullPool)
DB_POOL_SIZE = _env_int("DB_POOL_SIZE", 5)
DB_MAX_OVERFLOW = _env_int("DB_MAX_OVERFLOW", 10)
DB_POOL_TIMEOUT = _env_float("DB_POOL_TIMEOUT", 30.0)
DB_POOL_RECYCLE = _env_int("DB_POOL_RECYCLE", 1800)


# Retention metrics feature flag — disable to stop event logging without code change
RETENTION_METRICS_ENABLED: bool = os.getenv("RETENTION_METRICS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}


def session_secret_is_weak(secret: Optional[str] = None) -> bool:
    """Return True for known-default or too-short session secrets."""
    value = secret if secret is not None else SESSION_SECRET_KEY
    return not value or value == DEFAULT_SESSION_SECRET_KEY or len(value) < 32
