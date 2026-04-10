from app.db import SessionLocal
from app.models import Wallet
from app.ingest import ingest_trades

print("Fetching latest trades for all wallets...")
db = SessionLocal()
try:
    wallets = db.query(Wallet).all()
    print(f"Found {len(wallets)} wallets")
    
    for wallet in wallets:
        print(f"\nRefreshing {wallet.address[:10]}...")
        count = ingest_trades(db, wallet.address)
        print(f"Added {count} new trades")
        
finally:
    db.close()

print("\nDone! Check the database for new trades.")
