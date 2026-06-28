#!/usr/bin/env python
# Summary: Discovers active Polymarket wallets and optionally watches them.
# Details: It is a command-line helper for setup, maintenance, migration, backup, scoring, or operational checks.
"""Discover Polymarket wallets and grow the tracked universe.

Two discovery sources:
  * --source feed  (default): sweep the public ``/trades`` feed and rank wallets
    by recent volume.
  * --source profit | volume: pull *proven* traders from Polymarket's public
    leaderboard, ranked by all-time (or windowed) realized PnL / traded volume.
    This is the best way to add big, profitable wallets worth tracking.

With --add the new wallets are inserted into the watchlist; with --ingest their
full trade history is then fetched (this can take a while and makes many API
calls). Safe to re-run: existing wallets are never duplicated.

Usage (from the repo root):
    # Top all-time profitable traders, add them and ingest their history:
    python scripts/discover_wallets.py --source profit --window all \
        --max-add 100 --add --ingest

    # Recent high-volume feed sweep (original behaviour):
    python scripts/discover_wallets.py --pages 10 --add

Without --add it is a read-only preview (no DB writes, no ingest).
"""
import argparse
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover Polymarket wallets to track.")
    parser.add_argument("--source", choices=("feed", "profit", "volume"), default="feed",
                        help="feed=recent trade sweep; profit/volume=public leaderboard (default: feed).")
    parser.add_argument("--window", choices=("1d", "7d", "30d", "all"), default="all",
                        help="Leaderboard window for --source profit/volume (default: all).")
    parser.add_argument("--limit", type=int, default=200, help="Leaderboard ranks to request (default: 200).")
    parser.add_argument("--pages", type=int, default=10, help="Feed pages to sweep (default: 10).")
    parser.add_argument("--page-size", type=int, default=500, help="Trades per page (max 500).")
    parser.add_argument("--min-volume", type=float, default=0.0, help="Skip wallets below this recent USDC volume.")
    parser.add_argument("--min-trades", type=int, default=1, help="Skip feed wallets below this recent trade count.")
    parser.add_argument("--max-add", type=int, default=200, help="Cap wallets added with --add (default: 200).")
    parser.add_argument("--top", type=int, default=30, help="Rows to print in the preview (default: 30).")
    parser.add_argument("--add", action="store_true", help="Insert new wallets into the watchlist.")
    parser.add_argument("--ingest", action="store_true", help="After --add, fetch full history for the new wallets.")
    args = parser.parse_args()

    from app.db import get_db_context, init_db
    from app.discovery import (
        add_discovered_wallets,
        discover_active_wallets,
        discover_leaderboard_wallets,
        tracked_addresses,
    )

    # Leaderboard candidates carry no per-wallet trade count, so don't threshold
    # them on it; they're proven traders by construction. Tag by source so the
    # watchlist stays filterable.
    is_leaderboard = args.source in ("profit", "volume")
    min_trades = 0 if is_leaderboard else args.min_trades
    tag = {"profit": "profitable", "volume": "high-volume"}.get(args.source, "discovered")

    with get_db_context() as db:
        init_db()
        already = tracked_addresses(db)
        if is_leaderboard:
            print(f"Pulling top {args.limit} '{args.source}' leaderboard wallet(s) (window={args.window})...")
            discovered = discover_leaderboard_wallets(
                kind=args.source, window=args.window, limit=args.limit, exclude=already
            )
        else:
            print(f"Sweeping up to {args.pages} feed page(s) of {args.page_size} trades...")
            discovered = discover_active_wallets(pages=args.pages, page_size=args.page_size, exclude=already)

        print(f"\nFound {len(discovered)} candidate wallet(s) not already tracked "
              f"({len(already)} already on the watchlist).\n")
        metric = "profit" if args.source == "profit" else "volume"
        header = f"{'#':>3}  {'address':<44} {metric:>16} {'trades':>7} {'mkts':>5}  name"
        print(header)
        print("-" * len(header))
        for i, w in enumerate(discovered[: args.top], start=1):
            amount = w.profit if args.source == "profit" else w.volume
            print(f"{i:>3}  {w.address:<44} ${amount:>14,.0f} {w.trade_count:>7} {w.market_count:>5}  {w.name[:30]}")

        if not args.add:
            print("\n(read-only preview - pass --add to insert these wallets, --ingest to also fetch history)")
            return 0

        result = add_discovered_wallets(
            db,
            discovered,
            min_volume=args.min_volume,
            min_trades=min_trades,
            max_add=args.max_add,
            tag=tag,
        )
        print(
            f"\nAdded {result['added_count']} wallet(s). "
            f"Skipped {result['skipped_existing']} existing, "
            f"{result['skipped_threshold']} below threshold, "
            f"{result['skipped_invalid']} invalid."
        )
        new_addresses = list(result["added"])

    if args.ingest and new_addresses:
        from app.db import get_db_context as _ctx
        from app.ingest import refresh_wallet
        from app.models import Wallet

        print(f"\nIngesting full history for {len(new_addresses)} new wallet(s)...")
        for index, address in enumerate(new_addresses, start=1):
            with _ctx() as db:
                wallet = db.query(Wallet).filter(Wallet.address == address).first()
                if wallet is None:
                    continue
                outcome = refresh_wallet(db, wallet, fetch_all=True)
            print(f"  [{index}/{len(new_addresses)}] {address[:12]}... "
                  f"status={outcome['status']} inserted={outcome['inserted']}")
        print("\nDone. Next: python scripts/backfill_resolutions.py --resolutions "
              "&& python scripts/train_outcome_model.py")

    return 0


if __name__ == "__main__":
    sys.exit(main())
