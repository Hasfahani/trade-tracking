# Summary: Loads a bundled trades+resolutions seed into an empty database.
# Details: Makes a fresh deployment (e.g. Render free tier, whose Postgres can be
# wiped) show a populated leaderboard/dashboard without a manual login+refresh.
"""Seed real trade history into an empty database.

The seed file (``data/seed_trades.json``) is a real, deduped export of public
Polymarket trades plus their market resolutions. It is loaded ONLY when the
``trades`` table is empty, so it never touches a database that already has data.
Trades are marked ``alert_sent=1`` so seeding historical rows never triggers
Telegram alerts.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import MarketResolution, Trade, Wallet
from app.settings import DATA_DIR

logger = logging.getLogger(__name__)

DEFAULT_SEED_PATH = DATA_DIR / "seed_trades.json"


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO timestamp to a naive-UTC datetime (the DB stores naive)."""
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def seed_trades(db: Session, path: Path = DEFAULT_SEED_PATH) -> dict:
    """Insert bundled trades/resolutions when the trades table is empty.

    Returns a summary dict. Safe and cheap to call on every startup: it no-ops
    when trades already exist or the seed file is absent.
    """
    result = {"skipped": True, "reason": None, "inserted_trades": 0, "inserted_resolutions": 0}

    existing = db.query(func.count(Trade.id)).scalar() or 0
    if existing:
        result["reason"] = f"trades already present ({existing})"
        return result

    if not path.exists():
        result["reason"] = f"seed file missing ({path})"
        return result

    payload = json.loads(path.read_text(encoding="utf-8"))

    # Resolutions first (no FK dependency); skip any already present.
    existing_res = {cid for (cid,) in db.query(MarketResolution.condition_id).all()}
    res_rows = [
        {
            "condition_id": r["condition_id"],
            "market_title": r.get("market_title"),
            "outcome": r.get("outcome") or "UNRESOLVED",
            "resolved_at": _parse_dt(r.get("resolved_at")),
        }
        for r in payload.get("resolutions", [])
        if r["condition_id"] not in existing_res
    ]
    if res_rows:
        db.bulk_insert_mappings(MarketResolution, res_rows)

    # Trades only for wallets that exist (respect the FK); seeded wallets are
    # added earlier in startup maintenance.
    known_wallets = {addr for (addr,) in db.query(Wallet.address).all()}
    trade_rows = []
    skipped_no_wallet = 0
    for t in payload.get("trades", []):
        addr = t["wallet_address"]
        if addr not in known_wallets:
            skipped_no_wallet += 1
            continue
        trade_rows.append({
            "wallet_address": addr,
            "trade_id": t["trade_id"],
            "condition_id": t["condition_id"],
            "market_title": t.get("market_title"),
            "side": t["side"],
            "price": t["price"],
            "size": t["size"],
            "traded_at": _parse_dt(t.get("traded_at")),
            "outcome_token": t.get("outcome_token"),
            "notable_score": t.get("notable_score"),
            "alert_sent": 1,  # historical seed: never alert on these
        })
    if trade_rows:
        db.bulk_insert_mappings(Trade, trade_rows)

    db.flush()
    result.update(
        skipped=False,
        reason=None,
        inserted_trades=len(trade_rows),
        inserted_resolutions=len(res_rows),
        skipped_no_wallet=skipped_no_wallet,
    )
    return result
