"""Root redirect and dashboard routes."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import case, desc, func
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
    trade_value = Trade.price * Trade.size
    value_row = db.query(
        func.sum(trade_value).label("total_value"),
        func.sum(case((Trade.side == "YES", trade_value), else_=0)).label("yes_value"),
        func.sum(case((Trade.side == "NO", trade_value), else_=0)).label("no_value"),
    ).first()
    total_trade_value = float(value_row.total_value or 0)
    yes_value = float(value_row.yes_value or 0)
    no_value = float(value_row.no_value or 0)
    yes_value_pct = round((yes_value / total_trade_value) * 100) if total_trade_value else 0
    no_value_pct = 100 - yes_value_pct if total_trade_value else 0
    recent_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    recent_value_24h = float(
        db.query(func.sum(trade_value)).filter(Trade.traded_at >= recent_cutoff).scalar() or 0
    )
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
        {
            "wallet": wallet_map.get(row.wallet_address),
            "address": row.wallet_address,
            "trade_count": row.trade_count,
            "bar_pct": round((row.trade_count / top_wallets_rows[0].trade_count) * 100) if top_wallets_rows else 0,
        }
        for row in top_wallets_rows
    ]
    top_markets_rows = (
        db.query(
            Trade.condition_id,
            func.max(Trade.market_title).label("market_title"),
            func.count(Trade.id).label("trade_count"),
            func.sum(trade_value).label("total_value"),
        )
        .group_by(Trade.condition_id)
        .order_by(func.sum(trade_value).desc())
        .limit(5)
        .all()
    )
    top_market_value = float(top_markets_rows[0].total_value or 0) if top_markets_rows else 0
    top_markets = [
        {
            "condition_id": row.condition_id,
            "market": row.market_title or row.condition_id,
            "trade_count": int(row.trade_count or 0),
            "total_value": float(row.total_value or 0),
            "bar_pct": round((float(row.total_value or 0) / top_market_value) * 100) if top_market_value else 0,
        }
        for row in top_markets_rows
    ]
    today = datetime.now(timezone.utc).date()
    cutoff_day = today - timedelta(days=6)
    cutoff_7d = datetime(cutoff_day.year, cutoff_day.month, cutoff_day.day)
    daily_rows = (
        db.query(func.date(Trade.traded_at).label("trade_day"), func.count(Trade.id).label("trade_count"))
        .filter(Trade.traded_at >= cutoff_7d)
        .group_by(func.date(Trade.traded_at))
        .order_by(func.date(Trade.traded_at).asc())
        .all()
    )
    daily_map = {str(row.trade_day): int(row.trade_count or 0) for row in daily_rows}
    activity_days = []
    max_daily_count = max(daily_map.values(), default=0)
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        count = daily_map.get(day.isoformat(), 0)
        activity_days.append(
            {
                "label": day.strftime("%a"),
                "date": day.isoformat(),
                "count": count,
                "bar_pct": round((count / max_daily_count) * 100) if max_daily_count else 0,
            }
        )

    refresh_health = {
        "last_success_label": last_success_at.strftime("%Y-%m-%d %H:%M UTC") if last_success_at else "Never",
        "last_error_label": last_error_at.strftime("%Y-%m-%d %H:%M UTC") if last_error_at else "None recorded",
        "tone": "danger" if last_error_at and (not last_success_at or last_error_at > last_success_at) else "success",
    }
    interesting_activity = vh.detect_interesting_activity(db)
    insight_cards = [
        {
            "label": "24h value",
            "value": f"${recent_value_24h:,.2f}",
            "detail": "Stored trade value in the last day",
            "tone": "success" if recent_value_24h else "info",
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
            "total_wallets": total_wallets,
            "active_wallets_count": active_wallets_count,
            "archived_wallets_count": archived_wallets_count,
            "total_trades": total_trades,
            "total_trade_value": total_trade_value,
            "yes_value": yes_value,
            "no_value": no_value,
            "yes_value_pct": yes_value_pct,
            "no_value_pct": no_value_pct,
            "recent_value_24h": recent_value_24h,
            "last_success_at": last_success_at,
            "last_error_at": last_error_at,
            "refresh_health": refresh_health,
            "insight_cards": insight_cards,
            "recent_trades": recent_trades,
            "top_wallets": top_wallets,
            "top_markets": top_markets,
            "activity_days": activity_days,
            "wallet_map": wallet_map,
            "short_address": vh.short_address,
            "interesting_activity": interesting_activity,
        },
    )
