"""
Migrate data from a local SQLite database to a PostgreSQL database.

Usage:
    python scripts/migrate_to_postgres.py --sqlite data/app.db --postgres "postgresql://user:pass@host:5432/db"

The script is safe to re-run: every table uses INSERT ... ON CONFLICT DO NOTHING,
so rows that already exist in Postgres are silently skipped.

Migration order: wallets → trades → sync_events (respects no FK constraints,
but keeps logical ordering consistent with app startup).
"""

import argparse
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

# Allow running from the project root without installing the package
sys.path.insert(0, ".")

from app.models import Base, SyncEvent, Trade, Wallet  # noqa: E402

BATCH_SIZE = 500


def _engine(url: str):
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url)


def _row_count(conn, table: str) -> int:
    return conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def _migrate_wallets(src_session, dst_conn):
    rows = src_session.query(Wallet).order_by(Wallet.id).all()
    if not rows:
        print("  wallets: 0 rows — nothing to migrate")
        return 0

    inserted = 0
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
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
            }
            for w in batch
        ]
        stmt = pg_insert(Wallet).values(values).on_conflict_do_nothing(index_elements=["address"])
        result = dst_conn.execute(stmt)
        inserted += result.rowcount

    print(f"  wallets : {len(rows):>6} read  |  {inserted:>6} inserted  |  {len(rows) - inserted:>6} skipped")
    return inserted


def _migrate_trades(src_session, dst_conn):
    total = src_session.query(Trade).count()
    if total == 0:
        print("  trades  : 0 rows — nothing to migrate")
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
            }
            for t in batch
        ]
        stmt = pg_insert(Trade).values(values).on_conflict_do_nothing(index_elements=["trade_id"])
        result = dst_conn.execute(stmt)
        inserted += result.rowcount
        offset += len(batch)
        print(f"  trades  : {offset:>6}/{total} processed …", end="\r")

    print(f"  trades  : {total:>6} read  |  {inserted:>6} inserted  |  {total - inserted:>6} skipped      ")
    return inserted


def _migrate_sync_events(src_session, dst_conn):
    total = src_session.query(SyncEvent).count()
    if total == 0:
        print("  sync_events: 0 rows — nothing to migrate")
        return 0

    inserted = 0
    offset = 0
    while offset < total:
        batch = src_session.query(SyncEvent).order_by(SyncEvent.id).offset(offset).limit(BATCH_SIZE).all()
        if not batch:
            break
        values = [
            {
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
        # sync_events has no natural unique key — use id to avoid duplicates on re-runs
        stmt = (
            pg_insert(SyncEvent)
            .values(values)
            .on_conflict_do_nothing(index_elements=["id"])
        )
        result = dst_conn.execute(stmt)
        inserted += result.rowcount
        offset += len(batch)

    print(f"  sync_events: {total:>6} read  |  {inserted:>6} inserted  |  {total - inserted:>6} skipped")
    return inserted


def _utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime is timezone-aware (UTC). SQLite stores naive datetimes."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def main():
    parser = argparse.ArgumentParser(description="Migrate SQLite → PostgreSQL")
    parser.add_argument("--sqlite", default="data/app.db", help="Path to SQLite file (default: data/app.db)")
    parser.add_argument("--postgres", required=True, help="PostgreSQL connection URL")
    parser.add_argument("--dry-run", action="store_true", help="Read from SQLite but do not write to Postgres")
    args = parser.parse_args()

    sqlite_url = f"sqlite:///{args.sqlite}"
    pg_url = args.postgres
    if pg_url.startswith("postgres://"):
        pg_url = pg_url.replace("postgres://", "postgresql://", 1)

    print(f"\nSource : {sqlite_url}")
    print(f"Target : {pg_url}")
    if args.dry_run:
        print("Mode   : DRY RUN — no data will be written\n")
    else:
        print("Mode   : LIVE\n")

    src_engine = _engine(sqlite_url)
    dst_engine = _engine(pg_url)

    SrcSession = sessionmaker(bind=src_engine)
    src_session = SrcSession()

    print("Creating tables in Postgres (if they don't exist) …")
    Base.metadata.create_all(bind=dst_engine)

    if args.dry_run:
        print("\nDry-run counts from SQLite:")
        with src_engine.connect() as c:
            for table in ("wallets", "trades", "sync_events"):
                print(f"  {table}: {_row_count(c, table)}")
        src_session.close()
        return

    print("\nMigrating …")
    with dst_engine.begin() as dst_conn:
        _migrate_wallets(src_session, dst_conn)
        _migrate_trades(src_session, dst_conn)
        _migrate_sync_events(src_session, dst_conn)

    print("\nVerifying row counts in Postgres …")
    with dst_engine.connect() as c:
        for table in ("wallets", "trades", "sync_events"):
            print(f"  {table}: {_row_count(c, table)}")

    src_session.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
