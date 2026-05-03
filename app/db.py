from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from contextlib import contextmanager
from app.settings import DATABASE_URL
from app.models import Base

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _ensure_wallet_columns():
    """Add missing wallet columns for older SQLite databases."""
    if not DATABASE_URL.startswith("sqlite"):
        return

    expected_columns = {
        "tags": "TEXT",
        "notes": "TEXT",
        "is_pinned": "INTEGER",
        "is_archived": "INTEGER",
        "last_checked_at": "DATETIME",
        "last_refresh_status": "VARCHAR(32)",
        "last_refresh_count": "INTEGER",
        "last_error_at": "DATETIME",
        "last_error_message": "TEXT",
    }

    with engine.begin() as conn:
        table_exists = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='wallets'"
        ).first()
        if not table_exists:
            return

        rows = conn.exec_driver_sql("PRAGMA table_info(wallets)").fetchall()
        existing_columns = {row[1] for row in rows}

        for column_name, column_type in expected_columns.items():
            if column_name not in existing_columns:
                conn.exec_driver_sql(
                    f"ALTER TABLE wallets ADD COLUMN {column_name} {column_type}"
                )


def _ensure_sqlite_indexes():
    """Create lightweight indexes for older SQLite databases."""
    if not DATABASE_URL.startswith("sqlite"):
        return

    index_statements = [
        "CREATE INDEX IF NOT EXISTS ix_trades_wallet_traded_at ON trades (wallet_address, traded_at)",
        "CREATE INDEX IF NOT EXISTS ix_trades_wallet_side_traded_at ON trades (wallet_address, side, traded_at)",
        "CREATE INDEX IF NOT EXISTS ix_trades_wallet_market_title ON trades (wallet_address, market_title)",
        "CREATE INDEX IF NOT EXISTS ix_sync_events_wallet_created ON sync_events (wallet_address, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_wallets_archived_pinned_created ON wallets (is_archived, is_pinned, created_at)",
    ]

    with engine.begin() as conn:
        for statement in index_statements:
            conn.exec_driver_sql(statement)


def _ensure_sync_event_columns():
    """Add missing sync event columns for older SQLite databases."""
    if not DATABASE_URL.startswith("sqlite"):
        return

    expected_columns = {
        "duplicate_count": "INTEGER",
        "duration_ms": "INTEGER",
    }

    with engine.begin() as conn:
        table_exists = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sync_events'"
        ).first()
        if not table_exists:
            return

        rows = conn.exec_driver_sql("PRAGMA table_info(sync_events)").fetchall()
        existing_columns = {row[1] for row in rows}

        for column_name, column_type in expected_columns.items():
            if column_name not in existing_columns:
                conn.exec_driver_sql(
                    f"ALTER TABLE sync_events ADD COLUMN {column_name} {column_type}"
                )


def _ensure_settings_columns():
    """Add missing app_settings columns for older SQLite databases."""
    if not DATABASE_URL.startswith("sqlite"):
        return

    expected_columns = {
        "telegram_bot_token": "TEXT",
        "telegram_chat_id": "TEXT",
        "alert_min_size": "REAL",
        "alerts_enabled": "INTEGER",
        "updated_at": "DATETIME",
    }

    with engine.begin() as conn:
        table_exists = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='app_settings'"
        ).first()
        if not table_exists:
            return

        rows = conn.exec_driver_sql("PRAGMA table_info(app_settings)").fetchall()
        existing_columns = {row[1] for row in rows}

        for column_name, column_type in expected_columns.items():
            if column_name not in existing_columns:
                conn.exec_driver_sql(
                    f"ALTER TABLE app_settings ADD COLUMN {column_name} {column_type}"
                )


def init_db():
    """Initialize database tables."""
    Base.metadata.create_all(bind=engine)
    _ensure_wallet_columns()
    _ensure_sync_event_columns()
    _ensure_settings_columns()
    _ensure_sqlite_indexes()


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
