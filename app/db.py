from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, Optional, Set, Tuple

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, SyncEvent
from app.settings import DATABASE_URL

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

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


SCHEMA_MIGRATIONS: Tuple[Migration, ...] = (
    ("001_wallet_compat_columns", _migrate_wallet_columns),
    ("002_sync_event_columns", _migrate_sync_event_columns),
    ("003_app_settings_columns", _migrate_settings_columns),
    ("004_trade_alert_sent", _migrate_trade_alert_sent),
    ("005_sqlite_indexes", _migrate_sqlite_indexes),
    ("006_updated_at_columns", _migrate_updated_at_columns),
)


def _ensure_postgres_trade_columns(target_engine: Engine) -> None:
    with target_engine.begin() as conn:
        row = conn.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'trades' AND column_name = 'alert_sent'"
        ).first()
        if row is None:
            conn.exec_driver_sql("ALTER TABLE trades ADD COLUMN alert_sent INTEGER NOT NULL DEFAULT 0")


def run_schema_migrations(
    target_engine: Optional[Engine] = None,
    *,
    database_url: str = DATABASE_URL,
    migrations: Iterable[Migration] = SCHEMA_MIGRATIONS,
) -> None:
    """Run lightweight, tracked compatibility migrations for local SQLite databases."""
    target_engine = target_engine or engine
    if not _is_sqlite(database_url):
        _ensure_postgres_trade_columns(target_engine)
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
