from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.db import POSTGRES_COMPAT_COLUMNS, SCHEMA_MIGRATIONS, run_schema_migrations


def _sqlite_engine():
    return create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _columns(conn, table_name):
    return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()}


def test_schema_migrations_upgrade_legacy_sqlite_tables_and_record_versions():
    engine = _sqlite_engine()
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE wallets (
                id INTEGER PRIMARY KEY,
                address VARCHAR(255) NOT NULL UNIQUE,
                label TEXT,
                created_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                wallet_address VARCHAR(255) NOT NULL,
                trade_id VARCHAR(255) NOT NULL UNIQUE,
                condition_id VARCHAR(255) NOT NULL,
                market_title TEXT,
                side VARCHAR(3) NOT NULL,
                price FLOAT NOT NULL,
                size FLOAT NOT NULL,
                traded_at DATETIME NOT NULL,
                inserted_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE sync_events (
                id INTEGER PRIMARY KEY,
                wallet_address VARCHAR(255),
                status VARCHAR(32),
                fetched_count INTEGER,
                inserted_count INTEGER,
                error_message TEXT,
                created_at DATETIME
            )
            """
        )
        conn.exec_driver_sql("CREATE TABLE app_settings (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql(
            """
            INSERT INTO trades (
                wallet_address, trade_id, condition_id, market_title, side, price, size, traded_at
            ) VALUES (
                '0x1111111111111111111111111111111111111111',
                'legacy-trade',
                'legacy-condition',
                'Legacy Market',
                'YES',
                0.5,
                10,
                '2026-01-01 12:00:00'
            )
            """
        )

    run_schema_migrations(engine, database_url="sqlite://")
    run_schema_migrations(engine, database_url="sqlite://")

    with engine.begin() as conn:
        wallet_columns = _columns(conn, "wallets")
        trade_columns = _columns(conn, "trades")
        sync_columns = _columns(conn, "sync_events")
        settings_columns = _columns(conn, "app_settings")
        applied_versions = {
            row[0] for row in conn.exec_driver_sql("SELECT version FROM schema_migrations").fetchall()
        }
        indexes = {row[1] for row in conn.exec_driver_sql("PRAGMA index_list(trades)").fetchall()}
        alert_sent = conn.exec_driver_sql(
            "SELECT alert_sent FROM trades WHERE trade_id = 'legacy-trade'"
        ).scalar()

    assert {"tags", "notes", "is_pinned", "is_archived", "last_checked_at", "last_refresh_status"} <= wallet_columns
    assert "alert_sent" in trade_columns
    assert {"duplicate_count", "duration_ms"} <= sync_columns
    assert {"telegram_bot_token", "telegram_chat_id", "alert_min_size", "alerts_enabled"} <= settings_columns
    assert applied_versions == {version for version, _ in SCHEMA_MIGRATIONS}
    assert "ix_trades_wallet_traded_at" in indexes
    assert alert_sent == 0


def test_schema_migrations_create_tracking_table_even_without_app_tables():
    engine = _sqlite_engine()

    run_schema_migrations(engine, database_url="sqlite://")

    with engine.begin() as conn:
        applied_versions = [
            row[0] for row in conn.exec_driver_sql("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        ]

    assert applied_versions == [version for version, _ in SCHEMA_MIGRATIONS]


def test_postgres_compat_migrations_add_missing_columns_once():
    class _FakeResult:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    class _FakeConn:
        def __init__(self):
            self.columns = {
                "wallets": {"address", "created_at"},
                "trades": {"trade_id", "wallet_address"},
                "sync_events": {"status", "created_at"},
                "app_settings": {"id"},
            }
            self.alters = []

        def exec_driver_sql(self, statement, params=None):
            if "information_schema.columns" in statement:
                table_name = params["table_name"]
                column_name = params["column_name"]
                exists = column_name in self.columns.get(table_name, set())
                return _FakeResult((column_name,) if exists else None)
            if statement.startswith("ALTER TABLE"):
                self.alters.append(statement)
                parts = statement.split()
                table_name = parts[2]
                column_name = parts[5]
                self.columns.setdefault(table_name, set()).add(column_name)
                return _FakeResult(None)
            raise AssertionError(f"Unexpected SQL: {statement}")

    class _FakeBegin:
        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            return self._conn

        def __exit__(self, exc_type, exc, tb):
            return False

    class _FakeEngine:
        def __init__(self):
            self.conn = _FakeConn()

        def begin(self):
            return _FakeBegin(self.conn)

    fake_engine = _FakeEngine()

    run_schema_migrations(fake_engine, database_url="postgresql+psycopg://example")
    first_run_alters = list(fake_engine.conn.alters)
    run_schema_migrations(fake_engine, database_url="postgresql+psycopg://example")

    expected_column_count = sum(len(cols) for cols in POSTGRES_COMPAT_COLUMNS.values())
    assert len(first_run_alters) == expected_column_count
    assert len(fake_engine.conn.alters) == expected_column_count
