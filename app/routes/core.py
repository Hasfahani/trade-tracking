"""Root redirect and dashboard routes."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import SyncEvent, Trade, Wallet
from app.settings import APP_NAME
from app import view_helpers as vh
from app.routes._shared import templates

router = APIRouter()


@router.get("/")
async def root():
    return RedirectResponse(url="/wallets", status_code=302)


@router.get("/dashboard")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    total_wallets = db.query(func.count(Wallet.id)).scalar() or 0
    active_wallets_count = (
        db.query(func.count(Wallet.id)).filter(func.coalesce(Wallet.is_archived, 0) == 0).scalar() or 0
    )
    archived_wallets_count = (
        db.query(func.count(Wallet.id)).filter(func.coalesce(Wallet.is_archived, 0) == 1).scalar() or 0
    )
    total_trades = db.query(func.count(Trade.id)).scalar() or 0
    last_success_at = (
        db.query(func.max(SyncEvent.created_at)).filter(SyncEvent.status == "success").scalar()
    )
    last_error_at = (
        db.query(func.max(SyncEvent.created_at)).filter(SyncEvent.status == "error").scalar()
    )
    recent_trades = db.query(Trade).order_by(Trade.traded_at.desc()).limit(20).all()
    top_wallets_rows = (
        db.query(Trade.wallet_address, func.count(Trade.id).label("trade_count"))
        .group_by(Trade.wallet_address)
        .order_by(func.count(Trade.id).desc())
        .limit(5)
        .all()
    )
    wallet_map = {w.address: w for w in db.query(Wallet).all()}
    top_wallets = [
        {"wallet": wallet_map.get(row.wallet_address), "address": row.wallet_address, "trade_count": row.trade_count}
        for row in top_wallets_rows
    ]
    interesting_activity = vh.detect_interesting_activity(db)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "total_wallets": total_wallets,
            "active_wallets_count": active_wallets_count,
            "archived_wallets_count": archived_wallets_count,
            "total_trades": total_trades,
            "last_success_at": last_success_at,
            "last_error_at": last_error_at,
            "recent_trades": recent_trades,
            "top_wallets": top_wallets,
            "wallet_map": wallet_map,
            "short_address": vh.short_address,
            "interesting_activity": interesting_activity,
        },
    )
