import sys
sys.path.insert(0, '.')
from app.db import SessionLocal
from app.models import Wallet, Trade
from datetime import datetime

db = SessionLocal()

# Count wallets
wallet_count = db.query(Wallet).count()
print(f"\n=== DATABASE STATUS ===")
print(f"Wallets: {wallet_count}")

# List wallets
wallets = db.query(Wallet).all()
for w in wallets:
    print(f"  - {w.address[:10]}...{w.address[-8:]} ({w.label or 'No label'})")
    trade_count = db.query(Trade).filter(Trade.wallet_address == w.address).count()
    print(f"    Trades: {trade_count}")
    
    # Latest trade
    latest = db.query(Trade).filter(Trade.wallet_address == w.address).order_by(Trade.traded_at.desc()).first()
    if latest:
        print(f"    Latest: {latest.traded_at} - {latest.side} {latest.size}")
    else:
        print(f"    Latest: None")

# Total trades
total_trades = db.query(Trade).count()
print(f"\nTotal trades in DB: {total_trades}")

# Latest trade overall
latest_overall = db.query(Trade).order_by(Trade.traded_at.desc()).first()
if latest_overall:
    print(f"Most recent trade: {latest_overall.traded_at}")
    print(f"Current time: {datetime.utcnow()}")
    age = datetime.utcnow() - latest_overall.traded_at.replace(tzinfo=None)
    print(f"Age: {age}")

db.close()
