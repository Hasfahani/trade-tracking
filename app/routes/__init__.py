"""Route package aggregate."""
from fastapi import APIRouter

from app.routes import alerts, auth, core, exports, trades, wallets

router = APIRouter()
router.include_router(auth.router)
router.include_router(core.router)
router.include_router(exports.router)
router.include_router(wallets.router)
router.include_router(trades.router)
router.include_router(alerts.router)

__all__ = ["alerts", "auth", "core", "exports", "trades", "wallets", "router"]
