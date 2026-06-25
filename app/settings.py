# Summary: Loads app settings from environment variables.
# Details: It supports the FastAPI backend by keeping one main piece of app behavior clear and reusable.
import os
import subprocess
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# Base directory
BASE_DIR = Path(__file__).resolve().parent.parent


def _read_git_commit() -> str:
    """Return the current git commit SHA, preferring the env var set at build/deploy time."""
    commit = (os.getenv("GIT_COMMIT") or os.getenv("RENDER_GIT_COMMIT") or "").strip()
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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
GIT_COMMIT = _read_git_commit()
APP_VERSION = _env_str("APP_VERSION", GIT_COMMIT if GIT_COMMIT != "unknown" else "dev")
LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()
_platform_env_default = "production" if (os.getenv("RENDER") or os.getenv("RAILWAY_ENVIRONMENT")) else "development"
APP_ENV = _env_str("APP_ENV", _platform_env_default).lower()
IS_PRODUCTION = APP_ENV in {"production", "prod"} or bool(os.getenv("RENDER") or os.getenv("RAILWAY_ENVIRONMENT"))
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or ""
).rstrip("/")
RUNTIME_PLATFORM = _env_str(
    "RUNTIME_PLATFORM",
    "render" if os.getenv("RENDER") else "railway" if os.getenv("RAILWAY_ENVIRONMENT") else "docker" if os.getenv("DOCKER_CONTAINER") else "local",
)

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

# Authentication - if set, all routes require a session login
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "").strip() or None
PUBLIC_READ_ONLY = _env_bool("PUBLIC_READ_ONLY", False)

# Session secret key for itsdangerous / Starlette sessions
DEFAULT_SESSION_SECRET_KEY = "change-me-in-production-use-a-long-random-secret"
SESSION_SECRET_KEY = os.getenv("SESSION_SECRET_KEY", DEFAULT_SESSION_SECRET_KEY)

# Cookie/security behavior. Hosted platforms terminate TLS before forwarding to
# the app, so production defaults should emit Secure cookies even if uvicorn sees HTTP.
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
STARTUP_SEED_WALLETS = _env_bool("STARTUP_SEED_WALLETS", True)

# Connection pool tuning (PostgreSQL only; SQLite uses StaticPool/NullPool)
DB_POOL_SIZE = _env_int("DB_POOL_SIZE", 5)
DB_MAX_OVERFLOW = _env_int("DB_MAX_OVERFLOW", 10)
DB_POOL_TIMEOUT = _env_float("DB_POOL_TIMEOUT", 30.0)
DB_POOL_RECYCLE = _env_int("DB_POOL_RECYCLE", 1800)


# Retention metrics feature flag - disable to stop event logging without code change
RETENTION_METRICS_ENABLED: bool = os.getenv("RETENTION_METRICS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}

# AI Analysis - Anthropic Claude (preferred), Ollama (local), or HuggingFace
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = _env_str("ANTHROPIC_MODEL", "claude-sonnet-4-6")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral:latest").strip() or None
OLLAMA_TIMEOUT_SECONDS = _env_float("OLLAMA_TIMEOUT_SECONDS", 60.0)
HUGGINGFACE_API_KEY = os.getenv("HUGGINGFACE_API_KEY", "")
AI_CACHE_TTL_HOURS = _env_int("AI_CACHE_TTL_HOURS", 72)

# Rate limiting - applied to expensive endpoints (AI analysis, wallet refresh)
AI_RATE_LIMIT = _env_str("AI_RATE_LIMIT", "20/minute")
REFRESH_RATE_LIMIT = _env_str("REFRESH_RATE_LIMIT", "10/minute")

# Scheduled auto-refresh - set interval > 0 to enable background wallet polling
AUTO_REFRESH_INTERVAL_MINUTES = _env_int("AUTO_REFRESH_INTERVAL_MINUTES", 0)
AUTO_REFRESH_MAX_WALLETS = _env_int("AUTO_REFRESH_MAX_WALLETS", 50)
AUTO_REFRESH_ALLOW_MULTI_WORKER = _env_bool("AUTO_REFRESH_ALLOW_MULTI_WORKER", False)
WEB_CONCURRENCY = _env_int("WEB_CONCURRENCY", _env_int("UVICORN_WORKERS", 1))


def ai_provider_configured() -> bool:
    """Return True when config points to a likely reachable AI backend."""
    if ANTHROPIC_API_KEY or HUGGINGFACE_API_KEY:
        return True

    if not OLLAMA_MODEL:
        return False

    parsed = urlparse(OLLAMA_BASE_URL)
    host = (parsed.hostname or "").lower()
    if not host:
        return False

    if IS_PRODUCTION and host in {"localhost", "127.0.0.1", "::1"}:
        return False

    return True


def ai_unavailable_message() -> str:
    """Return a user-facing explanation for unavailable AI analysis."""
    if IS_PRODUCTION and not ai_provider_configured():
        return (
            "AI is not configured for this deployment. Add ANTHROPIC_API_KEY, "
            "set HUGGINGFACE_API_KEY, or point OLLAMA_BASE_URL to a reachable Ollama service."
        )
    return (
        "AI unavailable - set ANTHROPIC_API_KEY, run Ollama locally, or set "
        "HUGGINGFACE_API_KEY."
    )


def session_secret_is_weak(secret: Optional[str] = None) -> bool:
    """Return True for known-default or too-short session secrets."""
    value = secret if secret is not None else SESSION_SECRET_KEY
    return not value or value == DEFAULT_SESSION_SECRET_KEY or len(value) < 32
