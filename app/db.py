# Connects to the database and updates tables.
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, Optional, Set, Tuple

from sqlalchemy import Engine, create_engine
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.models import Base, EventLog, SyncEvent
from app.settings import (
    DATABASE_URL,
    DB_MAX_OVERFLOW,
    DB_POOL_RECYCLE,
    DB_POOL_SIZE,
    DB_POOL_TIMEOUT,
)

logger = logging.getLogger(__name__)

_is_sqlite_url = DATABASE_URL.startswith("sqlite")
_connect_args = (
    {"check_same_thread": False}
    if _is_sqlite_url
    else {"connect_timeout": 10}  # seconds; prevents indefinite hang when DB is not yet ready
)
_pool_kwargs = (
    {}
    if _is_sqlite_url
    else {
        "pool_size": DB_POOL_SIZE,
        "max_overflow": DB_MAX_OVERFLOW,
        "pool_timeout": DB_POOL_TIMEOUT,
        "pool_recycle": DB_POOL_RECYCLE,
    }
)
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True, **_pool_kwargs)
_SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
_schema_init_lock = threading.Lock()
_schema_initialized = False

ColumnSpec = Dict[str, str]
Migration = Tuple[str, Callable[[object], None]]


def _is_sqlite(database_url: str) -> bool:
    return database_url.startswith("sqlite")


def _table_exists(conn, table_name: str) -> bool:
    return bool(
        conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).first()
    )


def _column_names(conn, table_name: str) -> Set[str]:
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def _add_missing_columns(conn, table_name: str, expected_columns: ColumnSpec) -> None:
    if not _table_exists(conn, table_name):
        return

    existing_columns = _column_names(conn, table_name)
    for column_name, column_type in expected_columns.items():
        if column_name not in existing_columns:
            conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            logger.info("Migration: added column %s.%s (%s)", table_name, column_name, column_type)


def _create_schema_migrations_table(conn) -> None:
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _applied_migration_versions(conn) -> Set[str]:
    return {row[0] for row in conn.exec_driver_sql("SELECT version FROM schema_migrations").fetchall()}


def _record_migration(conn, version: str) -> None:
    conn.exec_driver_sql("INSERT INTO schema_migrations (version) VALUES (?)", (version,))


def _migrate_wallet_columns(conn) -> None:
    _add_missing_columns(
        conn,
        "wallets",
        {
            "tags": "TEXT",
            "notes": "TEXT",
            "is_pinned": "INTEGER",
            "is_archived": "INTEGER",
            "last_checked_at": "DATETIME",
            "last_refresh_status": "VARCHAR(32)",
            "last_refresh_count": "INTEGER",
            "last_error_at": "DATETIME",
            "last_error_message": "TEXT",
        },
    )


def _migrate_sync_event_columns(conn) -> None:
    _add_missing_columns(
        conn,
        "sync_events",
        {
            "duplicate_count": "INTEGER",
            "duration_ms": "INTEGER",
        },
    )


def _migrate_settings_columns(conn) -> None:
    _add_missing_columns(
        conn,
        "app_settings",
        {
            "telegram_bot_token": "TEXT",
            "telegram_chat_id": "TEXT",
            "alert_min_size": "REAL",
            "alerts_enabled": "INTEGER",
            "updated_at": "DATETIME",
        },
    )


def _migrate_trade_alert_sent(conn) -> None:
    _add_missing_columns(
        conn,
        "trades",
        {
            "alert_sent": "INTEGER NOT NULL DEFAULT 0",
        },
    )


def _migrate_updated_at_columns(conn) -> None:
    _add_missing_columns(conn, "wallets", {"updated_at": "DATETIME"})
    _add_missing_columns(conn, "trades", {"updated_at": "DATETIME"})


def _migrate_retention_indexes(conn) -> None:
    index_statements = [
        (
            "event_log",
            "CREATE INDEX IF NOT EXISTS ix_event_log_tracker_ts ON event_log (tracker_id, event_ts)",
        ),
        (
            "event_log",
            "CREATE INDEX IF NOT EXISTS ix_event_log_name_ts ON event_log (event_name, event_ts)",
        ),
        (
            "event_log",
            "CREATE INDEX IF NOT EXISTS ix_event_log_event_ts ON event_log (event_ts)",
        ),
    ]
    for table_name, statement in index_statements:
        if _table_exists(conn, table_name):
            conn.exec_driver_sql(statement)


def _migrate_sqlite_indexes(conn) -> None:
    index_statements = [
        (
            "trades",
            "CREATE INDEX IF NOT EXISTS ix_trades_wallet_traded_at ON trades (wallet_address, traded_at)",
        ),
        (
            "trades",
            "CREATE INDEX IF NOT EXISTS ix_trades_wallet_side_traded_at ON trades (wallet_address, side, traded_at)",
        ),
        (
            "trades",
            "CREATE INDEX IF NOT EXISTS ix_trades_wallet_market_title ON trades (wallet_address, market_title)",
        ),
        (
            "sync_events",
            "CREATE INDEX IF NOT EXISTS ix_sync_events_wallet_created ON sync_events (wallet_address, created_at)",
        ),
        (
            "wallets",
            "CREATE INDEX IF NOT EXISTS ix_wallets_archived_pinned_created ON wallets (is_archived, is_pinned, created_at)",
        ),
    ]

    for table_name, statement in index_statements:
        if _table_exists(conn, table_name):
            conn.exec_driver_sql(statement)


def _migrate_trade_analysis_table(conn) -> None:
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS trade_analysis (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id VARCHAR(255) NOT NULL UNIQUE,
            provider VARCHAR(64),
            signal VARCHAR(32),
            risk VARCHAR(16),
            price_insight TEXT,
            behavior TEXT,
            verdict TEXT,
            context_json TEXT,
            model_version VARCHAR(128),
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_trade_analysis_trade_id ON trade_analysis (trade_id)"
    )


def _migrate_trades_condition_traded_at_index(conn) -> None:
    if _table_exists(conn, "trades"):
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_trades_condition_traded_at ON trades (condition_id, traded_at)"
        )


def _migrate_trade_notable_score(conn) -> None:
    _add_missing_columns(
        conn,
        "trades",
        {
            "notable_score": "REAL",
        },
    )


def _migrate_wallet_ai_summary_columns(conn) -> None:
    _add_missing_columns(
        conn,
        "wallets",
        {
            "ai_summary": "TEXT",
            "ai_summary_at": "DATETIME",
        },
    )


def _migrate_trade_outcome_token(conn) -> None:
    _add_missing_columns(conn, "trades", {"outcome_token": "VARCHAR(3)"})


def _migrate_market_resolutions_table(conn) -> None:
    conn.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS market_resolutions (
            condition_id VARCHAR(255) PRIMARY KEY,
            market_title TEXT,
            outcome VARCHAR(16) NOT NULL DEFAULT 'UNRESOLVED',
            resolved_at DATETIME,
            checked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_market_resolutions_outcome ON market_resolutions (outcome)"
    )


SCHEMA_MIGRATIONS: Tuple[Migration, ...] = (
    ("001_wallet_compat_columns", _migrate_wallet_columns),
    ("002_sync_event_columns", _migrate_sync_event_columns),
    ("003_app_settings_columns", _migrate_settings_columns),
    ("004_trade_alert_sent", _migrate_trade_alert_sent),
    ("005_sqlite_indexes", _migrate_sqlite_indexes),
    ("006_updated_at_columns", _migrate_updated_at_columns),
    ("007_retention_indexes", _migrate_retention_indexes),
    ("008_trade_analysis_table", _migrate_trade_analysis_table),
    ("009_trades_condition_traded_at_index", _migrate_trades_condition_traded_at_index),
    ("010_wallet_ai_summary_columns", _migrate_wallet_ai_summary_columns),
    ("011_trade_notable_score", _migrate_trade_notable_score),
    ("012_trade_outcome_token", _migrate_trade_outcome_token),
    ("013_market_resolutions_table", _migrate_market_resolutions_table),
)


POSTGRES_COMPAT_COLUMNS: Dict[str, ColumnSpec] = {
    "trade_analysis": {
        "trade_id": "VARCHAR(255)",
        "provider": "VARCHAR(64)",
        "signal": "VARCHAR(32)",
        "risk": "VARCHAR(16)",
        "price_insight": "TEXT",
        "behavior": "TEXT",
        "verdict": "TEXT",
        "context_json": "TEXT",
        "model_version": "VARCHAR(128)",
        "created_at": "TIMESTAMP",
    },
    "event_log": {
        "tracker_id": "VARCHAR(64)",
        "event_name": "VARCHAR(64)",
        "event_ts": "TIMESTAMP",
        "route": "VARCHAR(255)",
        "metadata_json": "TEXT",
        "alert_id": "VARCHAR(255)",
    },
    "wallets": {
        "tags": "TEXT",
        "notes": "TEXT",
        "is_pinned": "INTEGER",
        "is_archived": "INTEGER",
        "last_checked_at": "TIMESTAMP",
        "last_refresh_status": "VARCHAR(32)",
        "last_refresh_count": "INTEGER",
        "last_error_at": "TIMESTAMP",
        "last_error_message": "TEXT",
        "updated_at": "TIMESTAMP",
        "ai_summary": "TEXT",
        "ai_summary_at": "TIMESTAMP",
    },
    "trades": {
        "alert_sent": "INTEGER NOT NULL DEFAULT 0",
        "updated_at": "TIMESTAMP",
        "notable_score": "DOUBLE PRECISION",
        "outcome_token": "VARCHAR(3)",
    },
    "market_resolutions": {
        "market_title": "TEXT",
        "outcome": "VARCHAR(16)",
        "resolved_at": "TIMESTAMP",
        "checked_at": "TIMESTAMP",
        "created_at": "TIMESTAMP",
    },
    "sync_events": {
        "duplicate_count": "INTEGER",
        "duration_ms": "INTEGER",
    },
    "app_settings": {
        "telegram_bot_token": "TEXT",
        "telegram_chat_id": "TEXT",
        "alert_min_size": "DOUBLE PRECISION",
        "alerts_enabled": "INTEGER",
        "updated_at": "TIMESTAMP",
    },
}

POSTGRES_INTEGER_COMPAT_COLUMNS: Tuple[Tuple[str, str], ...] = (
    ("wallets", "is_pinned"),
    ("wallets", "is_archived"),
    ("wallets", "last_refresh_count"),
    ("trades", "alert_sent"),
    ("sync_events", "duplicate_count"),
    ("sync_events", "duration_ms"),
    ("app_settings", "alerts_enabled"),
)


def _postgres_column_exists(conn, table_name: str, column_name: str) -> bool:
    row = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :table_name AND column_name = :column_name"
        ),
        {"table_name": table_name, "column_name": column_name},
    ).first()
    return row is not None


def _postgres_column_type(conn, table_name: str, column_name: str) -> Optional[str]:
    row = conn.execute(
        text(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = :table_name AND column_name = :column_name"
        ),
        {"table_name": table_name, "column_name": column_name},
    ).first()
    return row[0] if row else None


def _normalize_postgres_integer_columns(conn) -> None:
    for table_name, column_name in POSTGRES_INTEGER_COMPAT_COLUMNS:
        if _postgres_column_type(conn, table_name, column_name) != "boolean":
            continue
        conn.exec_driver_sql(
            f"ALTER TABLE {table_name} ALTER COLUMN {column_name} TYPE INTEGER "
            f"USING CASE WHEN {column_name} THEN 1 ELSE 0 END"
        )
        logger.info("Migration: converted %s.%s from boolean to integer", table_name, column_name)


def _upgrade_postgres_numeric_columns(conn) -> None:
    """Upgrade price and size columns from DOUBLE PRECISION to NUMERIC(18,6) on PostgreSQL."""
    for col in ("price", "size"):
        current_type = _postgres_column_type(conn, "trades", col)
        if current_type and current_type.lower() in ("double precision", "real", "float"):
            conn.exec_driver_sql(
                f"ALTER TABLE trades ALTER COLUMN {col} TYPE NUMERIC(18,6) USING {col}::NUMERIC(18,6)"
            )
            logger.info("Migration: upgraded trades.%s from %s to NUMERIC(18,6)", col, current_type)


def _ensure_postgres_compat_columns(target_engine: Engine) -> None:
    with target_engine.begin() as conn:
        for table_name, expected_columns in POSTGRES_COMPAT_COLUMNS.items():
            for column_name, column_type in expected_columns.items():
                if _postgres_column_exists(conn, table_name, column_name):
                    continue
                conn.exec_driver_sql(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
                logger.info("Migration: added column %s.%s (%s)", table_name, column_name, column_type)
        _normalize_postgres_integer_columns(conn)
        _upgrade_postgres_numeric_columns(conn)


def run_schema_migrations(
    target_engine: Optional[Engine] = None,
    *,
    database_url: str = DATABASE_URL,
    migrations: Iterable[Migration] = SCHEMA_MIGRATIONS,
) -> None:
    """Run lightweight, tracked compatibility migrations for local SQLite databases."""
    target_engine = target_engine or engine
    if not _is_sqlite(database_url):
        _ensure_postgres_compat_columns(target_engine)
        return

    with target_engine.begin() as conn:
        _create_schema_migrations_table(conn)
        applied = _applied_migration_versions(conn)
        for version, migration in migrations:
            if version in applied:
                continue
            migration(conn)
            _record_migration(conn, version)


def init_db() -> None:
    """Initialize database tables and apply tracked compatibility migrations."""
    Base.metadata.create_all(bind=engine)
    run_schema_migrations()


def ensure_database_initialized() -> None:
    """Initialize schema once per process before opening ad-hoc sessions.

    This keeps direct scripts and any early request paths from failing on newly
    added compatibility tables such as ``trade_analysis``.
    """
    global _schema_initialized
    if _schema_initialized:
        return

    with _schema_init_lock:
        if _schema_initialized:
            return
        init_db()
        _schema_initialized = True


def SessionLocal():
    """Return a database session with lazy one-time schema initialization."""
    ensure_database_initialized()
    return _SessionFactory()


def check_database_ready(target_engine: Optional[Engine] = None) -> None:
    """Raise if the configured database cannot execute a trivial query."""
    target_engine = target_engine or engine
    with target_engine.connect() as conn:
        conn.execute(text("SELECT 1"))


@contextmanager
def get_db_context():
    """Context manager for database sessions."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db():
    """Dependency for FastAPI routes."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_applied_migration_versions() -> list:
    """Return list of applied schema migration version strings, or empty list if table missing."""
    try:
        with engine.connect() as conn:
            rows = conn.exec_driver_sql("SELECT version FROM schema_migrations ORDER BY version").fetchall()
            return [row[0] for row in rows]
    except Exception:
        return []


def prune_old_sync_events(db, keep_days: int = 90) -> int:
    """Delete sync events older than ``keep_days`` days.

    Args:
        db: An active SQLAlchemy session.
        keep_days: Events older than this many days are removed.

    Returns:
        Number of rows deleted.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=keep_days)
    deleted = db.query(SyncEvent).filter(SyncEvent.created_at < cutoff).delete(synchronize_session=False)
    if deleted:
        db.commit()
    return int(deleted)
