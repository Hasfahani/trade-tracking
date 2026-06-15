#!/usr/bin/env python3
# Imports app data from a backup file.
"""Import a PolySignal JSON backup into SQLite or PostgreSQL."""

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.backup import BACKUP_MODELS, import_backup_payload, validate_backup_payload  # noqa: E402
from app.models import Base  # noqa: E402
from app.settings import DATABASE_URL  # noqa: E402

def _engine(url: str):
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(url)


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
    try:
        tables = validate_backup_payload(payload)
    except ValueError as exc:
        raise SystemExit(str(exc))
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
    session = sessionmaker(bind=engine)()

    try:
        summary = import_backup_payload(session, payload)
    finally:
        session.close()

    for table_name, count in summary.table_counts.items():
        inserted = summary.inserted_counts.get(table_name, 0)
        print(f"{table_name}: {count} read, {inserted} inserted, {count - inserted} skipped")

    print(f"Done. Inserted {summary.inserted_total} rows.")


if __name__ == "__main__":
    main()
