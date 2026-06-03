"""Portable JSON backup helpers for all application-owned tables."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from typing import Any

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
