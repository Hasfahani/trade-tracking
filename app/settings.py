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


# Database
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'app.db'}")

# App metadata
APP_NAME = _env_str("APP_NAME", "Polymarket Wallet Watchlist")
LOG_LEVEL = _env_str("LOG_LEVEL", "INFO").upper()

# Server runtime
HOST = os.getenv("HOST", "0.0.0.0")
PORT = _env_int("PORT", 8000)

# Pagination defaults
DEFAULT_PAGE_SIZE = _env_int("DEFAULT_PAGE_SIZE", 50)
MAX_PAGE_SIZE = _env_int("MAX_PAGE_SIZE", 200)

# Ingestion behavior
DEFAULT_REFRESH_LIMIT = _env_int("DEFAULT_REFRESH_LIMIT", 200)
