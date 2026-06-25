#!/usr/bin/env python
# Summary: Backfills outcome tokens and resolved market outcomes.
# Details: It is a command-line helper for setup, maintenance, migration, backup, scoring, or operational checks.
"""Backfill realized-PnL inputs for existing trades.

Two best-effort passes (both on by default), against the local/prod database:

1. --tokens: re-fetch each wallet's trades and set outcome_token on rows that
   predate that column (only fills NULLs; never changes existing values).
2. --resolutions: fetch resolved outcomes (Polymarket CLOB API) for the distinct
   markets the wallets traded that aren't resolved yet, and upsert them.

After this, wallet pages and the leaderboard show real PnL/ROI/win rate for the
markets that have resolved. Safe to re-run.

Usage (from the repo root):
    python scripts/backfill_resolutions.py [--tokens] [--resolutions]
                                           [--wallet 0x..] [--limit N]
"""
import argparse
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill outcome tokens and market resolutions.")
    parser.add_argument("--tokens", action="store_true", help="Backfill outcome_token on existing trades (re-fetches trades).")
    parser.add_argument("--resolutions", action="store_true", help="Fetch and store resolved market outcomes.")
    parser.add_argument("--wallet", default=None, help="Restrict to a single wallet address.")
    parser.add_argument("--limit", type=int, default=400, help="Max markets to resolve this run (default: 400).")
    args = parser.parse_args()
    # Default: run both passes when neither flag is given.
    do_tokens = args.tokens or not (args.tokens or args.resolutions)
    do_resolutions = args.resolutions or not (args.tokens or args.resolutions)

    from app.db import SessionLocal
    from app.ingest import (
        _backfill_outcome_tokens,
        fetch_market_resolution,
        fetch_trades_for_wallet,
        normalize_trade,
        upsert_market_resolution,
        _CONDITION_ID_RE,
    )
    from app.models import MarketResolution, Trade
    from sqlalchemy import func

    db = SessionLocal()
    try:
        wallet_filter = [args.wallet.lower()] if args.wallet else [
            row[0] for row in db.query(Trade.wallet_address).distinct().all()
        ]

        if do_tokens:
            print(f"[tokens] Backfilling outcome_token for {len(wallet_filter)} wallet(s)...")
            for index, address in enumerate(wallet_filter, start=1):
                try:
                    raw = fetch_trades_for_wallet(address, fetch_all=True)
                except Exception as exc:
                    print(f"  [{index}/{len(wallet_filter)}] {address[:10]}... fetch failed: {exc}")
                    continue
                normalized = [t for t in (normalize_trade(r, address) for r in raw) if t]
                ids = {t["id"] for t in normalized}
                _backfill_outcome_tokens(db, normalized, ids)
                db.commit()
                filled = db.query(func.count(Trade.id)).filter(
                    Trade.wallet_address == address, Trade.outcome_token.isnot(None)
                ).scalar()
                print(f"  [{index}/{len(wallet_filter)}] {address[:10]}... fetched={len(normalized)} tokened_total={filled}")

        if do_resolutions:
            resolved = {
                cid for (cid,) in db.query(MarketResolution.condition_id)
                .filter(MarketResolution.outcome.in_(("YES", "NO"))).all()
            }
            condition_query = db.query(Trade.condition_id).distinct()
            if args.wallet:
                condition_query = condition_query.filter(Trade.wallet_address == args.wallet.lower())
            candidates = [
                cid for (cid,) in condition_query.all()
                if cid not in resolved and _CONDITION_ID_RE.match(cid or "")
            ][: args.limit]
            print(f"[resolutions] Resolving up to {len(candidates)} market(s)...")
            newly_resolved = 0
            for index, condition_id in enumerate(candidates, start=1):
                data = fetch_market_resolution(condition_id)
                if data is None:
                    continue
                upsert_market_resolution(db, data)
                if data["outcome"] in ("YES", "NO"):
                    newly_resolved += 1
                if index % 25 == 0:
                    db.commit()
                    print(f"  ...{index}/{len(candidates)} checked, {newly_resolved} resolved")
            db.commit()
            print(f"[resolutions] Done. {newly_resolved} markets resolved (YES/NO).")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
