# Backs up and restores app data.
"""Portable JSON backup helpers for all application-owned tables."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import Date, DateTime
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import (
    AppSettings,
    EventLog,
    RetentionDaily,
    RetentionWeekly,
    SyncEvent,
    Trade,
    TradeAnalysis,
    Wallet,
)
from app.settings import APP_NAME, APP_VERSION, GIT_COMMIT

BACKUP_FORMAT_VERSION = 1

BACKUP_MODELS = (
    Wallet,
    Trade,
    SyncEvent,
    AppSettings,
    EventLog,
    RetentionDaily,
    RetentionWeekly,
    TradeAnalysis,
)

CONFLICT_KEYS = {
    "wallets": ["address"],
    "trades": ["trade_id"],
    "trade_analysis": ["trade_id"],
}


@dataclass(frozen=True)
class ImportSummary:
    table_counts: dict[str, int]
    inserted_counts: dict[str, int]

    @property
    def inserted_total(self) -> int:
        return sum(self.inserted_counts.values())


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _model_rows(db: Session, model) -> list[dict[str, Any]]:
    primary_keys = [column.name for column in model.__table__.primary_key.columns]
    order_columns = [getattr(model, name) for name in primary_keys] or list(model.__table__.columns)[:1]
    rows = db.query(model).order_by(*order_columns).all()
    columns = [column.name for column in model.__table__.columns]
    return [
        {column: _json_value(getattr(row, column)) for column in columns}
        for row in rows
    ]


def build_backup(db: Session) -> dict[str, Any]:
    """Return a deterministic JSON-compatible backup payload."""
    tables = {model.__tablename__: _model_rows(db, model) for model in BACKUP_MODELS}
    counts = {name: len(rows) for name, rows in tables.items()}
    checksum_source = json.dumps(tables, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    checksum = hashlib.sha256(checksum_source.encode("utf-8")).hexdigest()
    return {
        "format": "polysignal.backup",
        "format_version": BACKUP_FORMAT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "app": {
            "name": APP_NAME,
            "version": APP_VERSION,
            "commit": GIT_COMMIT,
        },
        "counts": counts,
        "checksum_sha256": checksum,
        "tables": tables,
    }


def backup_json(db: Session) -> str:
    """Return an indented JSON backup string."""
    return json.dumps(build_backup(db), ensure_ascii=False, indent=2, sort_keys=True)


def validate_backup_payload(payload: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Validate a backup payload and return its table data."""
    if payload.get("format") != "polysignal.backup":
        raise ValueError("Not a PolySignal backup file.")
    if payload.get("format_version") != BACKUP_FORMAT_VERSION:
        raise ValueError(f"Unsupported backup format version: {payload.get('format_version')}")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise ValueError("Backup file is missing table data.")
    return tables


def _parse_value(column, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(column.type, DateTime):
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    if isinstance(column.type, Date):
        return date.fromisoformat(value) if isinstance(value, str) else value
    return value


def _prepare_rows(model, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    columns = {column.name: column for column in model.__table__.columns}
    prepared = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        prepared.append(
            {
                name: _parse_value(columns[name], value)
                for name, value in row.items()
                if name in columns
            }
        )
    return prepared


def _conflict_keys(model) -> list[str]:
    return CONFLICT_KEYS.get(
        model.__tablename__,
        [column.name for column in model.__table__.primary_key.columns],
    )


def _insert_statement(bind, model, rows: list[dict[str, Any]]):
    dialect = bind.dialect.name
    if dialect == "postgresql":
        return pg_insert(model).values(rows).on_conflict_do_nothing(index_elements=_conflict_keys(model))
    if dialect == "sqlite":
        return sqlite_insert(model).values(rows).on_conflict_do_nothing(index_elements=_conflict_keys(model))
    raise RuntimeError(f"Unsupported database dialect: {dialect}")


def import_backup_payload(db: Session, payload: dict[str, Any], *, batch_size: int = 250) -> ImportSummary:
    """Import a backup payload into the active database, skipping duplicates."""
    tables = validate_backup_payload(payload)
    table_counts = {model.__tablename__: len(tables.get(model.__tablename__, [])) for model in BACKUP_MODELS}
    inserted_counts: dict[str, int] = {}
    bind = db.get_bind()

    for model in BACKUP_MODELS:
        table_name = model.__tablename__
        rows = _prepare_rows(model, tables.get(table_name, []))
        inserted = 0
        for index in range(0, len(rows), batch_size):
            batch = rows[index : index + batch_size]
            if not batch:
                continue
            result = db.execute(_insert_statement(bind, model, batch))
            inserted += result.rowcount or 0
        inserted_counts[table_name] = inserted

    db.commit()
    return ImportSummary(table_counts=table_counts, inserted_counts=inserted_counts)
