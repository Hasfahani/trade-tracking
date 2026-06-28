#!/usr/bin/env python
# Summary: Fetches full trade history for many tracked wallets at once.
# Details: It is a command-line helper for setup, maintenance, migration, backup, scoring, or operational checks.
"""Bulk-ingest full trade history for the tracked wallets.

Walks the watchlist and runs the normal per-wallet refresh (fetch_all=True) for
each, with progress, a small inter-wallet pause to stay polite to the API, and
a summary. This is the data engine behind growing the ML training set: after
discovering and adding wallets, run this to pull their history, then backfill
resolutions and retrain.

Usage (from the repo root):
    python scripts/bulk_ingest.py [--only-new] [--only-stale HOURS]
                                  [--limit N] [--pause 0.5] [--no-resolutions]

By default every tracked wallet is refreshed. --only-new skips wallets that
already have trades; --only-stale skips wallets checked within HOURS.
"""
import argparse
import sys
import time
import pathlib
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="Bulk-ingest full history for tracked wallets.")
    parser.add_argument("--only-new", action="store_true", help="Only ingest wallets that currently have no trades.")
    parser.add_argument("--only-stale", type=float, default=None, metavar="HOURS",
                        help="Only ingest wallets last checked more than HOURS ago.")
    parser.add_argument("--limit", type=int, default=None, help="Max wallets to ingest this run.")
    parser.add_argument("--pause", type=float, default=0.5, help="Seconds to pause between wallets (default: 0.5).")
    parser.add_argument("--no-resolutions", action="store_true", help="Skip the per-wallet resolution fetch (faster).")
    args = parser.parse_args()

    from app.db import get_db_context
    from app.ingest import refresh_wallet
    from app.models import Trade, Wallet
    from sqlalchemy import func

    with get_db_context() as db:
        wallets_with_trades = {
            row[0] for row in db.query(Trade.wallet_address).distinct().all()
        }
        stale_cutoff = None
        if args.only_stale is not None:
            stale_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=args.only_stale)

        targets = []
        for wallet in db.query(Wallet).order_by(Wallet.is_pinned.desc(), Wallet.address).all():
            if args.only_new and wallet.address in wallets_with_trades:
                continue
            if stale_cutoff is not None and wallet.last_checked_at and wallet.last_checked_at > stale_cutoff:
                continue
            targets.append(wallet.address)
        if args.limit is not None:
            targets = targets[: args.limit]

    total = len(targets)
    print(f"Bulk-ingesting {total} wallet(s) (fetch_all=True, resolutions={not args.no_resolutions})...\n")

    inserted_total = 0
    errors = 0
    for index, address in enumerate(targets, start=1):
        started = time.monotonic()
        with get_db_context() as db:
            wallet = db.query(Wallet).filter(Wallet.address == address).first()
            if wallet is None:
                continue
            result = refresh_wallet(db, wallet, fetch_all=True, fetch_resolutions=not args.no_resolutions)
        inserted_total += int(result.get("inserted", 0))
        if result.get("status") == "error":
            errors += 1
        elapsed = time.monotonic() - started
        print(f"  [{index}/{total}] {address[:12]}... status={result['status']:<8} "
              f"fetched={result['fetched']:>5} inserted={result['inserted']:>5} ({elapsed:4.1f}s)")
        if args.pause and index < total:
            time.sleep(args.pause)

    print(f"\nDone. {inserted_total} new trades inserted across {total} wallet(s); {errors} error(s).")
    if inserted_total:
        print("Next: python scripts/backfill_resolutions.py --resolutions "
              "&& python scripts/train_outcome_model.py && python scripts/train_model.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
