#!/usr/bin/env python3
"""Seed the curated Polymarket wallet watchlist.

This script is idempotent:
- missing wallets are inserted
- existing wallets are updated in place
- no duplicate wallet rows are created
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.db import get_db_context, init_db
from app.watchlist_seed import seed_watchlist_wallets


if __name__ == "__main__":
    print("Initializing database...")
    init_db()
    with get_db_context() as db:
        result = seed_watchlist_wallets(db)
    print(
        f"Seed complete. Added {result['added']} wallets, updated {result['updated']} wallets, "
        f"processed {result['total']} curated wallets."
    )
