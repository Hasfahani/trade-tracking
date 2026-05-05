"""Trade listing and detail routes."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import view_helpers as vh
from app.db import get_db
from app.models import Trade, Wallet
from app.routes._shared import normalized_date_filters, paginated_query, resolve_wallet, templates
from app.settings import APP_NAME, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter()


@router.get("/wallets/{identifier}/trades")
async def view_trades(
    request: Request,
    identifier: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    side: Optional[str] = Query(None),
    market_search: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    date_preset: Optional[str] = Query(None),
    sort_by: str = Query("time_desc"),
    db: Session = Depends(get_db),
):
    wallet = resolve_wallet(db, identifier)
    date_from, date_to = normalized_date_filters(date_preset, date_from, date_to)

    base_query = vh.apply_trade_filters(
        db.query(Trade),
        wallet_address=wallet.address,
        side=side,
        market_search=market_search,
        date_from=date_from,
        date_to=date_to,
    )
    sorted_query = vh.sorted_trade_query(base_query, sort_by)

    page, total_trades, total_pages, pagination, trades = paginated_query(sorted_query, page, page_size)
    summary_row = base_query.with_entities(
        func.min(Trade.traded_at).label("oldest_trade_at"),
        func.max(Trade.traded_at).label("newest_trade_at"),
    ).first()
    pnl = vh.trade_pnl_summary(base_query)
    activity_timeline = vh.build_wallet_activity_timeline(db, wallet.address)

    return templates.TemplateResponse(
        request,
        "trades_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "wallet": wallet,
            "trades": trades,
            "page": page,
            "page_size": page_size,
            "total_trades": total_trades,
            "total_pages": total_pages,
            "pagination": pagination,
            "side": side,
            "market_search": market_search,
            "date_from": date_from,
            "date_to": date_to,
            "date_preset": date_preset,
            "sort_by": sort_by,
            "summary_row": summary_row,
            "pnl": pnl,
            "activity_timeline": activity_timeline,
            "short_address": vh.short_address,
            "duration_label": vh.duration_label,
            "flash": request.query_params.get("flash"),
            "flash_level": request.query_params.get("level", "info"),
        },
    )


@router.get("/all-trades")
async def all_trades(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    side: Optional[str] = Query(None),
    market_search: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    date_preset: Optional[str] = Query(None),
    wallet_search: Optional[str] = Query(None),
    sort_by: str = Query("time_desc"),
    db: Session = Depends(get_db),
):
    date_from, date_to = normalized_date_filters(date_preset, date_from, date_to)

    query = vh.apply_trade_filters(
        db.query(Trade),
        side=side,
        market_search=market_search,
        date_from=date_from,
        date_to=date_to,
    )
    query = vh.apply_wallet_search_to_trade_query(db, query, wallet_search)
    query = vh.sorted_trade_query(query, sort_by)
    page, total_trades, total_pages, pagination, trades = paginated_query(query, page, page_size)
    summary_row = query.order_by(False).with_entities(
        func.min(Trade.traded_at).label("oldest_trade_at"),
        func.max(Trade.traded_at).label("newest_trade_at"),
    ).first()
    pnl = vh.trade_pnl_summary(query)
    wallet_map = {wallet.address: wallet for wallet in db.query(Wallet).all()}
    total_value = float(pnl["total_value"] or 0)
    yes_value = float(pnl["yes_value"] or 0)
    no_value = float(pnl["no_value"] or 0)
    yes_value_pct = round((yes_value / total_value) * 100) if total_value else 0
    no_value_pct = 100 - yes_value_pct if total_value else 0
    filtered_base = query.order_by(False)
    unique_wallets = int(filtered_base.with_entities(func.count(func.distinct(Trade.wallet_address))).scalar() or 0)
    unique_markets = int(filtered_base.with_entities(func.count(func.distinct(Trade.condition_id))).scalar() or 0)
    largest_trade = filtered_base.order_by((Trade.price * Trade.size).desc()).first()
    top_wallet_rows = (
        filtered_base.with_entities(
            Trade.wallet_address,
            func.count(Trade.id).label("trade_count"),
            func.sum(Trade.price * Trade.size).label("total_value"),
        )
        .group_by(Trade.wallet_address)
        .order_by(func.sum(Trade.price * Trade.size).desc())
        .limit(5)
        .all()
    )
    top_wallet_value = float(top_wallet_rows[0].total_value or 0) if top_wallet_rows else 0
    filtered_top_wallets = [
        {
            "address": row.wallet_address,
            "wallet": wallet_map.get(row.wallet_address),
            "trade_count": int(row.trade_count or 0),
            "total_value": float(row.total_value or 0),
            "bar_pct": round((float(row.total_value or 0) / top_wallet_value) * 100) if top_wallet_value else 0,
        }
        for row in top_wallet_rows
    ]
    top_market_rows = (
        filtered_base.with_entities(
            Trade.condition_id,
            func.max(Trade.market_title).label("market_title"),
            func.count(Trade.id).label("trade_count"),
            func.sum(Trade.price * Trade.size).label("total_value"),
        )
        .group_by(Trade.condition_id)
        .order_by(func.sum(Trade.price * Trade.size).desc())
        .limit(5)
        .all()
    )
    top_market_value = float(top_market_rows[0].total_value or 0) if top_market_rows else 0
    filtered_top_markets = [
        {
            "condition_id": row.condition_id,
            "market": row.market_title or row.condition_id,
            "trade_count": int(row.trade_count or 0),
            "total_value": float(row.total_value or 0),
            "bar_pct": round((float(row.total_value or 0) / top_market_value) * 100) if top_market_value else 0,
        }
        for row in top_market_rows
    ]
    filter_insights = [
        {
            "label": "Wallets",
            "value": str(unique_wallets),
            "detail": "Wallets matching the active filters",
            "tone": "info",
        },
        {
            "label": "Markets",
            "value": str(unique_markets),
            "detail": "Markets matching the active filters",
            "tone": "info",
        },
        {
            "label": "Largest trade",
            "value": f"${(largest_trade.price * largest_trade.size):,.2f}" if largest_trade else "$0.00",
            "detail": largest_trade.market_title or largest_trade.condition_id if largest_trade else "No matching trades",
            "tone": "success" if largest_trade else "info",
        },
    ]
    return templates.TemplateResponse(
        request,
        "all_trades_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "trades": trades,
            "page": page,
            "page_size": page_size,
            "total_trades": total_trades,
            "total_pages": total_pages,
            "pagination": pagination,
            "side": side,
            "market_search": market_search,
            "date_from": date_from,
            "date_to": date_to,
            "date_preset": date_preset,
            "wallet_search": wallet_search,
            "sort_by": sort_by,
            "summary_row": summary_row,
            "pnl": pnl,
            "yes_value_pct": yes_value_pct,
            "no_value_pct": no_value_pct,
            "filter_insights": filter_insights,
            "filtered_top_wallets": filtered_top_wallets,
            "filtered_top_markets": filtered_top_markets,
            "wallet_map": wallet_map,
            "short_address": vh.short_address,
        },
    )


@router.get("/trades/{trade_id}")
async def trade_detail(request: Request, trade_id: str, db: Session = Depends(get_db)):
    trade = db.query(Trade).filter(Trade.trade_id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    related_trades = (
        db.query(Trade)
        .filter(Trade.condition_id == trade.condition_id)
        .order_by(Trade.traded_at.desc())
        .limit(200)
        .all()
    )
    wallet_map = {wallet.address: wallet for wallet in db.query(Wallet).all()}
    return templates.TemplateResponse(
        request,
        "trade_detail_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "trade": trade,
            "related_trades": related_trades,
            "wallet_map": wallet_map,
            "short_address": vh.short_address,
        },
    )
