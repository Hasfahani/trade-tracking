from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routes_v2 import router
from app.db import init_db
import asyncio
from contextlib import asynccontextmanager

# Background task flag
_background_task = None


async def auto_refresh_trades():
    """Background task to periodically check for new trades every 2 minutes."""
    from app.db import SessionLocal
    from app.models import Wallet
    from app.ingest import refresh_wallet
    
    while True:
        try:
            await asyncio.sleep(120)  # Wait 2 minutes
            
            print(" Auto-refresh: Fetching new trades for all wallets...")
            db = SessionLocal()
            try:
                wallets = db.query(Wallet).all()
                total_new = 0
                
                for wallet in wallets:
                    result = refresh_wallet(db, wallet)
                    total_new += result["inserted"]
                    if result["inserted"] > 0:
                        print(f" Auto-refresh: {result['inserted']} new trades for wallet {wallet.address[:10]}...")
                    
                print(f" Auto-refresh complete: Added {total_new} new trades across {len(wallets)} wallets")
            finally:
                db.close()
        except Exception as e:
            print(f" Auto-refresh error: {e}")
            import traceback
            traceback.print_exc()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle - startup and shutdown."""
    global _background_task
    
    # Startup
    init_db()
    _background_task = asyncio.create_task(auto_refresh_trades())
    print(" Live tracking enabled: Checking for new trades every 2 minutes")
    
    yield
    
    # Shutdown
    if _background_task:
        _background_task.cancel()
        try:
            await _background_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(router)
