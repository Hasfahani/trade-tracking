#!/usr/bin/env python3
"""Import a PolySignal JSON backup into SQLite or PostgreSQL."""

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import Date, DateTime, create_engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.backup import BACKUP_FORMAT_VERSION, BACKUP_MODELS  # noqa: E402
from app.models import Base  # noqa: E402
from app.settings import DATABASE_URL  # noqa: E402

IMPORT_BATCH_SIZE = 250

CONFLICT_KEYS = {
    "wallets": ["address"],
    "trades": ["trade_id"],
    "trade_analysis": ["trade_id"],
}


def _engine(url: str):
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url)


def _parse_value(column, value):
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return datetime.fromisoformat(value)
    if isinstance(column.type, Date):
        return date.fromisoformat(value)
    return value


def _prepare_rows(model, rows):
    columns = {column.name: column for column in model.__table__.columns}
    prepared = []
    for row in rows:
        prepared.append(
            {
                name: _parse_value(columns[name], value)
                for name, value in row.items()
                if name in columns
            }
        )
    return prepared


def _conflict_keys(model):
    return CONFLICT_KEYS.get(
        model.__tablename__,
        [column.name for column in model.__table__.primary_key.columns],
    )


def _insert_statement(engine, model, rows):
    if engine.dialect.name == "postgresql":
        return pg_insert(model).values(rows).on_conflict_do_nothing(index_elements=_conflict_keys(model))
    if engine.dialect.name == "sqlite":
        return sqlite_insert(model).values(rows).on_conflict_do_nothing(index_elements=_conflict_keys(model))
    raise RuntimeError(f"Unsupported database dialect: {engine.dialect.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import a PolySignal JSON backup")
    parser.add_argument("backup", help="Path to backup JSON file")
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", DATABASE_URL),
        help="Target SQLAlchemy database URL. Defaults to DATABASE_URL/current app setting.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate and count rows without writing")
    parser.add_argument("--yes", action="store_true", help="Confirm import without an interactive prompt")
    args = parser.parse_args()

    payload = json.loads(Path(args.backup).read_text(encoding="utf-8"))
    if payload.get("format") != "polysignal.backup":
        raise SystemExit("Not a PolySignal backup file.")
    if payload.get("format_version") != BACKUP_FORMAT_VERSION:
        raise SystemExit(f"Unsupported backup format version: {payload.get('format_version')}")

    tables = payload.get("tables") or {}
    counts = {model.__tablename__: len(tables.get(model.__tablename__, [])) for model in BACKUP_MODELS}
    print(f"Backup: {args.backup}")
    print(f"Target: {args.database_url}")
    print(f"Rows  : {counts}")

    if args.dry_run:
        print("Dry run complete. No rows were written.")
        return

    if not args.yes:
        raise SystemExit("Refusing to import without --yes. Re-run with --dry-run first, then --yes.")

    engine = _engine(args.database_url)
    Base.metadata.create_all(bind=engine)

    inserted_total = 0
    with engine.begin() as conn:
        for model in BACKUP_MODELS:
            table_name = model.__tablename__
            rows = _prepare_rows(model, tables.get(table_name, []))
            if not rows:
                print(f"{table_name}: 0 rows")
                continue
            inserted = 0
            for index in range(0, len(rows), IMPORT_BATCH_SIZE):
                batch = rows[index : index + IMPORT_BATCH_SIZE]
                result = conn.execute(_insert_statement(engine, model, batch))
                inserted += result.rowcount or 0
            inserted_total += inserted
            print(f"{table_name}: {len(rows)} read, {inserted} inserted, {len(rows) - inserted} skipped")

    print(f"Done. Inserted {inserted_total} rows.")


if __name__ == "__main__":
    main()
