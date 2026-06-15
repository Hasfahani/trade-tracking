# Copies SQLite data into PostgreSQL.
"""
Migrate data from a local SQLite database to a PostgreSQL database.

Usage:
    python scripts/migrate_to_postgres.py --sqlite data/app.db --postgres "postgresql://user:pass@host:5432/db"

The script is safe to re-run: every table uses INSERT ... ON CONFLICT DO NOTHING,
so rows that already exist in Postgres are skipped.
"""

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, ".")

from app.models import (  # noqa: E402
    AppSettings,
    Base,
    EventLog,
    RetentionDaily,
    RetentionWeekly,
    SyncEvent,
    Trade,
    TradeAnalysis,
    Wallet,
)

BATCH_SIZE = 500
MIGRATED_TABLES = (
    "wallets",
    "trades",
    "sync_events",
    "app_settings",
    "event_log",
    "retention_daily",
    "retention_weekly",
    "trade_analysis",
)


def _engine(url: str):
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url)


def _row_count(conn, table: str) -> int:
    return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _migrate_wallets(src_session, dst_conn):
    rows = src_session.query(Wallet).order_by(Wallet.id).all()
    if not rows:
        print("  wallets: 0 rows - nothing to migrate")
        return 0

    inserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        values = [
            {
                "address": w.address,
                "label": w.label,
                "tags": w.tags,
                "notes": w.notes,
                "is_pinned": w.is_pinned or 0,
                "is_archived": w.is_archived or 0,
                "last_checked_at": _utc(w.last_checked_at),
                "last_refresh_status": w.last_refresh_status,
                "last_refresh_count": w.last_refresh_count,
                "last_error_at": _utc(w.last_error_at),
                "last_error_message": w.last_error_message,
                "created_at": _utc(w.created_at) or datetime.now(timezone.utc),
                "updated_at": _utc(w.updated_at),
            }
            for w in rows[i : i + BATCH_SIZE]
        ]
        result = dst_conn.execute(
            pg_insert(Wallet).values(values).on_conflict_do_nothing(index_elements=["address"])
        )
        inserted += result.rowcount

    print(f"  wallets: {len(rows):>6} read | {inserted:>6} inserted | {len(rows) - inserted:>6} skipped")
    return inserted


def _migrate_trades(src_session, dst_conn):
    total = src_session.query(Trade).count()
    if total == 0:
        print("  trades: 0 rows - nothing to migrate")
        return 0

    inserted = 0
    offset = 0
    while offset < total:
        batch = src_session.query(Trade).order_by(Trade.id).offset(offset).limit(BATCH_SIZE).all()
        if not batch:
            break
        values = [
            {
                "wallet_address": t.wallet_address,
                "trade_id": t.trade_id,
                "condition_id": t.condition_id,
                "market_title": t.market_title,
                "side": t.side,
                "price": t.price,
                "size": t.size,
                "traded_at": _utc(t.traded_at),
                "inserted_at": _utc(t.inserted_at) or datetime.now(timezone.utc),
                "updated_at": _utc(t.updated_at),
                "alert_sent": t.alert_sent or 0,
            }
            for t in batch
        ]
        result = dst_conn.execute(
            pg_insert(Trade).values(values).on_conflict_do_nothing(index_elements=["trade_id"])
        )
        inserted += result.rowcount
        offset += len(batch)

    print(f"  trades: {total:>6} read | {inserted:>6} inserted | {total - inserted:>6} skipped")
    return inserted


def _migrate_sync_events(src_session, dst_conn):
    total = src_session.query(SyncEvent).count()
    if total == 0:
        print("  sync_events: 0 rows - nothing to migrate")
        return 0

    inserted = 0
    offset = 0
    while offset < total:
        batch = src_session.query(SyncEvent).order_by(SyncEvent.id).offset(offset).limit(BATCH_SIZE).all()
        if not batch:
            break
        values = [
            {
                "id": e.id,
                "wallet_address": e.wallet_address,
                "status": e.status,
                "fetched_count": e.fetched_count,
                "inserted_count": e.inserted_count,
                "duplicate_count": e.duplicate_count,
                "duration_ms": e.duration_ms,
                "error_message": e.error_message,
                "created_at": _utc(e.created_at) or datetime.now(timezone.utc),
            }
            for e in batch
        ]
        result = dst_conn.execute(
            pg_insert(SyncEvent).values(values).on_conflict_do_nothing(index_elements=["id"])
        )
        inserted += result.rowcount
        offset += len(batch)

    print(f"  sync_events: {total:>6} read | {inserted:>6} inserted | {total - inserted:>6} skipped")
    return inserted


def _migrate_app_settings(src_session, dst_conn):
    rows = src_session.query(AppSettings).order_by(AppSettings.id).all()
    if not rows:
        print("  app_settings: 0 rows - nothing to migrate")
        return 0

    values = [
        {
            "id": row.id,
            "telegram_bot_token": row.telegram_bot_token,
            "telegram_chat_id": row.telegram_chat_id,
            "alert_min_size": row.alert_min_size,
            "alerts_enabled": row.alerts_enabled,
            "updated_at": _utc(row.updated_at),
        }
        for row in rows
    ]
    result = dst_conn.execute(
        pg_insert(AppSettings).values(values).on_conflict_do_nothing(index_elements=["id"])
    )
    print(f"  app_settings: {len(rows):>6} read | {result.rowcount:>6} inserted | {len(rows) - result.rowcount:>6} skipped")
    return result.rowcount


def _migrate_event_log(src_session, dst_conn):
    total = src_session.query(EventLog).count()
    if total == 0:
        print("  event_log: 0 rows - nothing to migrate")
        return 0

    inserted = 0
    offset = 0
    while offset < total:
        batch = src_session.query(EventLog).order_by(EventLog.id).offset(offset).limit(BATCH_SIZE).all()
        if not batch:
            break
        values = [
            {
                "id": e.id,
                "tracker_id": e.tracker_id,
                "event_name": e.event_name,
                "event_ts": _utc(e.event_ts) or datetime.now(timezone.utc),
                "route": e.route,
                "metadata_json": e.metadata_json,
                "alert_id": e.alert_id,
            }
            for e in batch
        ]
        result = dst_conn.execute(
            pg_insert(EventLog).values(values).on_conflict_do_nothing(index_elements=["id"])
        )
        inserted += result.rowcount
        offset += len(batch)

    print(f"  event_log: {total:>6} read | {inserted:>6} inserted | {total - inserted:>6} skipped")
    return inserted


def _migrate_retention_daily(src_session, dst_conn):
    rows = src_session.query(RetentionDaily).order_by(RetentionDaily.date).all()
    if not rows:
        print("  retention_daily: 0 rows - nothing to migrate")
        return 0

    values = [
        {
            "date": row.date,
            "dau": row.dau,
            "wau_rolling_7": row.wau_rolling_7,
            "alert_impressions_users": row.alert_impressions_users,
            "alert_open_users": row.alert_open_users,
            "alert_open_rate": row.alert_open_rate,
            "d1_return_rate": row.d1_return_rate,
            "d7_return_rate": row.d7_return_rate,
            "sessions_per_user": row.sessions_per_user,
            "computed_at": _utc(row.computed_at),
        }
        for row in rows
    ]
    result = dst_conn.execute(
        pg_insert(RetentionDaily).values(values).on_conflict_do_nothing(index_elements=["date"])
    )
    print(f"  retention_daily: {len(rows):>6} read | {result.rowcount:>6} inserted | {len(rows) - result.rowcount:>6} skipped")
    return result.rowcount


def _migrate_retention_weekly(src_session, dst_conn):
    rows = src_session.query(RetentionWeekly).order_by(RetentionWeekly.week_start).all()
    if not rows:
        print("  retention_weekly: 0 rows - nothing to migrate")
        return 0

    values = [
        {
            "week_start": row.week_start,
            "wau": row.wau,
            "repeat_users": row.repeat_users,
            "sessions_per_user": row.sessions_per_user,
            "alert_open_rate": row.alert_open_rate,
            "computed_at": _utc(row.computed_at),
        }
        for row in rows
    ]
    result = dst_conn.execute(
        pg_insert(RetentionWeekly).values(values).on_conflict_do_nothing(index_elements=["week_start"])
    )
    print(f"  retention_weekly: {len(rows):>6} read | {result.rowcount:>6} inserted | {len(rows) - result.rowcount:>6} skipped")
    return result.rowcount


def _migrate_trade_analysis(src_session, dst_conn):
    total = src_session.query(TradeAnalysis).count()
    if total == 0:
        print("  trade_analysis: 0 rows - nothing to migrate")
        return 0

    inserted = 0
    offset = 0
    while offset < total:
        batch = src_session.query(TradeAnalysis).order_by(TradeAnalysis.id).offset(offset).limit(BATCH_SIZE).all()
        if not batch:
            break
        values = [
            {
                "trade_id": row.trade_id,
                "provider": row.provider,
                "signal": row.signal,
                "risk": row.risk,
                "price_insight": row.price_insight,
                "behavior": row.behavior,
                "verdict": row.verdict,
                "context_json": row.context_json,
                "created_at": _utc(row.created_at),
                "model_version": row.model_version,
            }
            for row in batch
        ]
        result = dst_conn.execute(
            pg_insert(TradeAnalysis).values(values).on_conflict_do_nothing(index_elements=["trade_id"])
        )
        inserted += result.rowcount
        offset += len(batch)

    print(f"  trade_analysis: {total:>6} read | {inserted:>6} inserted | {total - inserted:>6} skipped")
    return inserted


def main():
    parser = argparse.ArgumentParser(description="Migrate SQLite to PostgreSQL")
    parser.add_argument("--sqlite", default="data/app.db", help="Path to SQLite file (default: data/app.db)")
    parser.add_argument("--postgres", required=True, help="PostgreSQL connection URL")
    parser.add_argument("--dry-run", action="store_true", help="Read from SQLite but do not write to Postgres")
    args = parser.parse_args()

    sqlite_url = f"sqlite:///{args.sqlite}"
    pg_url = args.postgres

    print(f"\nSource : {sqlite_url}")
    print(f"Target : {pg_url}")
    print("Mode   : DRY RUN - no data will be written\n" if args.dry_run else "Mode   : LIVE\n")

    src_engine = _engine(sqlite_url)
    if args.dry_run:
        print("\nDry-run counts from SQLite:")
        with src_engine.connect() as conn:
            for table in MIGRATED_TABLES:
                print(f"  {table}: {_row_count(conn, table)}")
        return

    dst_engine = _engine(pg_url)
    src_session = sessionmaker(bind=src_engine)()

    try:
        print("Creating tables in Postgres (if they don't exist)...")
        Base.metadata.create_all(bind=dst_engine)

        print("\nMigrating...")
        with dst_engine.begin() as dst_conn:
            _migrate_wallets(src_session, dst_conn)
            _migrate_trades(src_session, dst_conn)
            _migrate_sync_events(src_session, dst_conn)
            _migrate_app_settings(src_session, dst_conn)
            _migrate_event_log(src_session, dst_conn)
            _migrate_retention_daily(src_session, dst_conn)
            _migrate_retention_weekly(src_session, dst_conn)
            _migrate_trade_analysis(src_session, dst_conn)

        print("\nVerifying row counts in Postgres...")
        with dst_engine.connect() as conn:
            for table in MIGRATED_TABLES:
                print(f"  {table}: {_row_count(conn, table)}")
    finally:
        src_session.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
