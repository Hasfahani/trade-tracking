"""Root redirect and dashboard routes."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.responses import RedirectResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.db import check_database_ready, get_db
from app.models import SyncEvent, Trade, Wallet
from app.queries import get_dashboard_stats
from app.settings import APP_NAME, APP_ENV, IS_PRODUCTION
from app import view_helpers as vh
from app.routes._shared import templates

router = APIRouter()


@router.get("/")
async def root():
    return RedirectResponse(url="/wallets", status_code=302)


@router.get("/healthz")
async def healthz(request: Request):
    return JSONResponse(
        {
            "status": "ok",
            "app": APP_NAME,
            "env": APP_ENV,
            "production": IS_PRODUCTION,
            "ready": bool(getattr(request.app.state, "ready", False)),
        },
        status_code=200,
    )


@router.get("/readyz")
async def readyz(request: Request):
    ready = bool(getattr(request.app.state, "ready", False))
    db_ok = False
    error = getattr(request.app.state, "startup_error", None)
    if ready:
        try:
            check_database_ready()
            db_ok = True
        except Exception:
            ready = False
            error = "database check failed"
    status_code = 200 if ready and db_ok else 503
    return JSONResponse(
        {
            "status": "ready" if status_code == 200 else "not_ready",
            "database": "ok" if db_ok else "unavailable",
            "startup_error": error,
        },
        status_code=status_code,
    )


@router.get("/dashboard")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    stats = get_dashboard_stats(db)

    recent_trades = db.query(Trade).order_by(Trade.traded_at.desc()).limit(20).all()

    from sqlalchemy import func
    top_wallets_rows = (
        db.query(Trade.wallet_address, func.count(Trade.id).label("trade_count"))
        .group_by(Trade.wallet_address)
        .order_by(func.count(Trade.id).desc())
        .limit(5)
        .all()
    )
    needed_addresses = {row.wallet_address for row in top_wallets_rows} | {t.wallet_address for t in recent_trades}
    wallet_map = {w.address: w for w in db.query(Wallet).filter(Wallet.address.in_(needed_addresses)).all()}
    top_wallets = [
        {
            "wallet": wallet_map.get(row.wallet_address),
            "address": row.wallet_address,
            "trade_count": row.trade_count,
            "bar_pct": round((row.trade_count / top_wallets_rows[0].trade_count) * 100) if top_wallets_rows else 0,
        }
        for row in top_wallets_rows
    ]

    top_markets = vh.build_top_markets(db)
    activity_days = vh.build_activity_heatmap(db)

    last_success_at = stats["last_success_at"]
    last_error_at = stats["last_error_at"]
    latest_sync_event = db.query(SyncEvent).order_by(desc(SyncEvent.created_at)).first()
    refresh_health = {
        "last_success_label": last_success_at.strftime("%Y-%m-%d %H:%M UTC") if last_success_at else "Never",
        "last_error_label": last_error_at.strftime("%Y-%m-%d %H:%M UTC") if last_error_at else "None recorded",
        "latest_status": latest_sync_event.status if latest_sync_event else "none",
        "latest_status_label": (latest_sync_event.status or "unknown").replace("_", " ").title() if latest_sync_event else "No syncs yet",
        "latest_summary": (
            f"{latest_sync_event.inserted_count or 0} inserted from {latest_sync_event.fetched_count or 0} fetched"
            if latest_sync_event
            else "Run a wallet refresh to establish the first sync event."
        ),
        "latest_error": latest_sync_event.error_message if latest_sync_event and latest_sync_event.error_message else None,
        "tone": "danger" if last_error_at and (not last_success_at or last_error_at > last_success_at) else "success",
    }
    interesting_activity = vh.detect_interesting_activity(db)
    insight_cards = [
        {
            "label": "24h value",
            "value": f"${stats['recent_value_24h']:,.2f}",
            "detail": "Stored trade value in the last day",
            "tone": "success" if stats["recent_value_24h"] else "info",
        },
        {
            "label": "Interesting events",
            "value": str(len(interesting_activity)),
            "detail": "Large trades, spikes, and new markets",
            "tone": "warning" if interesting_activity else "info",
        },
        {
            "label": "Top market",
            "value": top_markets[0]["market"] if top_markets else "None yet",
            "detail": f"${top_markets[0]['total_value']:,.2f} stored value" if top_markets else "Refresh wallets to populate markets",
            "tone": "success" if top_markets else "info",
        },
    ]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "app_name": APP_NAME,
            **stats,
            "refresh_health": refresh_health,
            "insight_cards": insight_cards,
            "recent_trades": recent_trades,
            "top_wallets": top_wallets,
            "top_markets": top_markets,
            "activity_days": activity_days,
            "wallet_map": wallet_map,
            "short_address": vh.short_address,
            "sync_status_class": vh.sync_status_class,
            "interesting_activity": interesting_activity,
        },
    )
