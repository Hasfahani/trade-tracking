"""Trade listing and detail routes."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import view_helpers as vh
from app.db import get_db
from app.models import Trade, Wallet
from app.routes._shared import resolve_wallet, templates
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
    if date_preset in {"today", "7d", "30d"} and not date_from and not date_to:
        preset_range = vh.date_preset_range(date_preset)
        date_from = preset_range["date_from"]
        date_to = preset_range["date_to"]

    base_query = vh.apply_trade_filters(
        db.query(Trade),
        wallet_address=wallet.address,
        side=side,
        market_search=market_search,
        date_from=date_from,
        date_to=date_to,
    )
    sorted_query = vh.sorted_trade_query(base_query, sort_by)

    total_trades = sorted_query.count()
    total_pages = max(1, (total_trades + page_size - 1) // page_size)
    page = min(page, total_pages)
    pagination = vh.pagination_meta(page, page_size, total_trades)

    trades = sorted_query.limit(page_size).offset((page - 1) * page_size).all()
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
    if date_preset in {"today", "7d", "30d"} and not date_from and not date_to:
        preset_range = vh.date_preset_range(date_preset)
        date_from = preset_range["date_from"]
        date_to = preset_range["date_to"]

    query = vh.apply_trade_filters(
        db.query(Trade),
        side=side,
        market_search=market_search,
        date_from=date_from,
        date_to=date_to,
    )
    query = vh.apply_wallet_search_to_trade_query(db, query, wallet_search)
    query = vh.sorted_trade_query(query, sort_by)
    total_trades = query.count()
    total_pages = max(1, (total_trades + page_size - 1) // page_size)
    page = min(page, total_pages)
    pagination = vh.pagination_meta(page, page_size, total_trades)

    trades = query.limit(page_size).offset((page - 1) * page_size).all()
    summary_row = query.order_by(False).with_entities(
        func.min(Trade.traded_at).label("oldest_trade_at"),
        func.max(Trade.traded_at).label("newest_trade_at"),
    ).first()
    pnl = vh.trade_pnl_summary(query)
    wallet_map = {wallet.address: wallet for wallet in db.query(Wallet).all()}
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
