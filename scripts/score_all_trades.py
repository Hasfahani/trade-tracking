#!/usr/bin/env python
# Adds model scores to saved trades.
"""Backfill notable_score for all existing trades.

Loads the exported model weights (data/model_weights.json â€” numpy only, no
tensorflow needed), iterates wallets, scores each wallet's eligible trades,
and commits per wallet. By default only trades with a NULL notable_score are
updated; pass --overwrite to rescore everything.

Usage (from the repo root):
    python scripts/score_all_trades.py [--overwrite]
"""
import argparse
import sys

# Ensure repo root is on sys.path when run as a script
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill notable_score for all trades.")
    parser.add_argument("--overwrite", action="store_true", help="Recompute scores even when already set")
    args = parser.parse_args()

    from app.db import SessionLocal
    from app.ml.features import build_features_for_wallet
    from app.ml.model import DEFAULT_WEIGHTS_PATH, load_model
    from app.models import Trade

    model = load_model()
    if model is None:
        print(f"No usable model weights at {DEFAULT_WEIGHTS_PATH} â€” run scripts/train_model.py first.")
        return 1

    db = SessionLocal()
    try:
        wallet_addresses = [
            row[0]
            for row in db.query(Trade.wallet_address).distinct().order_by(Trade.wallet_address).all()
        ]
        print(f"Scoring trades for {len(wallet_addresses)} wallets...")
        total_scored = 0
        total_updated = 0
        for index, wallet_address in enumerate(wallet_addresses, start=1):
            trades = (
                db.query(Trade)
                .filter(Trade.wallet_address == wallet_address)
                .order_by(Trade.traded_at.asc(), Trade.id.asc())
                .all()
            )
            updated = 0
            X, _, trade_ids = build_features_for_wallet(trades)
            if trade_ids:
                scores = model.predict(X)
                trades_by_id = {trade.trade_id: trade for trade in trades}
                for trade_id, score in zip(trade_ids, scores):
                    trade = trades_by_id[trade_id]
                    if args.overwrite or trade.notable_score is None:
                        trade.notable_score = float(score)
                        updated += 1
                if updated:
                    db.commit()
            total_scored += len(trade_ids)
            total_updated += updated
            print(
                f"[{index}/{len(wallet_addresses)}] {wallet_address[:10]}... "
                f"trades={len(trades)} scorable={len(trade_ids)} updated={updated}"
            )
        print(f"Done. Scored {total_scored} trades, updated notable_score on {total_updated}.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
