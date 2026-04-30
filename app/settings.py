import os
from pathlib import Path

# Base directory
BASE_DIR = Path(__file__).resolve().parent.parent


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
LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()

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
