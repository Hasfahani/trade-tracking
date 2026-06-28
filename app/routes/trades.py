# Summary: Handles trade list and trade detail pages.
# Details: It connects browser requests to the right database work, page rendering, redirects, and JSON responses.
"""Trade listing and detail routes."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import ai_analysis, retention as ret, settings, view_helpers as vh
from app.db import get_db
from app.limiter import limiter
from app.models import Trade, Wallet
from app.routes._shared import normalized_date_filters, paginated_query, resolve_wallet, sanitize_search, validated_date_preset, templates
from app.settings import APP_NAME, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, RETENTION_METRICS_ENABLED, AI_RATE_LIMIT

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
    market_search = sanitize_search(market_search)
    date_preset = validated_date_preset(date_preset)
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
async def all_trades(  # noqa: PLR0913
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
    if RETENTION_METRICS_ENABLED:
        ret.emit(ret.RawEvent(
            tracker_id=ret.get_or_create_tracker_id(request),
            event_name="page_view",
            route="all_trades",
        ))

    wallet_search = sanitize_search(wallet_search)
    market_search = sanitize_search(market_search)
    date_preset = validated_date_preset(date_preset)
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
    filtered_base = query.order_by(False)

    page, total_trades, total_pages, pagination, trades = paginated_query(query, page, page_size)
    summary_row = filtered_base.with_entities(
        func.min(Trade.traded_at).label("oldest_trade_at"),
        func.max(Trade.traded_at).label("newest_trade_at"),
    ).first()
    pnl = vh.trade_pnl_summary(filtered_base)
    total_value = float(pnl["total_value"] or 0)
    yes_value = float(pnl["yes_value"] or 0)
    no_value = float(pnl["no_value"] or 0)
    yes_value_pct = round((yes_value / total_value) * 100) if total_value else 0
    no_value_pct = 100 - yes_value_pct if total_value else 0

    uniq_row = filtered_base.with_entities(
        func.count(func.distinct(Trade.wallet_address)).label("wallets"),
        func.count(func.distinct(Trade.condition_id)).label("markets"),
    ).first()
    unique_wallets = int(uniq_row.wallets or 0)
    unique_markets = int(uniq_row.markets or 0)
    largest_trade = filtered_base.order_by((Trade.price * Trade.size).desc()).first()
    filtered_top_wallets_raw = vh.build_filtered_top_wallets(filtered_base)
    filtered_top_markets = vh.build_filtered_top_markets(filtered_base)

    # Load only wallets needed for this page and the top-wallets panel
    needed_addresses = {t.wallet_address for t in trades} | {w["address"] for w in filtered_top_wallets_raw}
    wallet_map = {w.address: w for w in db.query(Wallet).filter(Wallet.address.in_(needed_addresses)).all()}
    filtered_top_wallets = [{"wallet": wallet_map.get(w["address"]), **w} for w in filtered_top_wallets_raw]

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
            "detail": (largest_trade.market_title or largest_trade.condition_id) if largest_trade else "No matching trades",
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

    if RETENTION_METRICS_ENABLED:
        ret.emit(ret.RawEvent(
            tracker_id=ret.get_or_create_tracker_id(request),
            event_name="trade_detail_view",
            route="trade_detail",
            metadata={"trade_id": trade_id},
        ))

    related_trades = (
        db.query(Trade)
        .filter(Trade.condition_id == trade.condition_id)
        .order_by(Trade.traded_at.desc())
        .limit(200)
        .all()
    )
    related_addresses = {t.wallet_address for t in related_trades}
    wallet_map = {w.address: w for w in db.query(Wallet).filter(Wallet.address.in_(related_addresses)).all()}

    from app.ml.model import get_signal_model

    local_model_loaded = get_signal_model() is not None
    ai_analysis_available = (
        local_model_loaded or trade.notable_score is not None or settings.ai_provider_configured()
    )
    if ai_analysis_available:
        ai_unavailable_message = ""
    elif settings.ai_provider_configured():
        ai_unavailable_message = settings.ai_unavailable_message()
    else:
        ai_unavailable_message = ai_analysis.local_unavailable_reason(trade, db)
    return templates.TemplateResponse(
        request,
        "trade_detail_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "trade": trade,
            "related_trades": related_trades,
            "wallet_map": wallet_map,
            "ai_provider_configured": settings.ai_provider_configured(),
            "local_model_loaded": local_model_loaded,
            "ai_analysis_available": ai_analysis_available,
            "ai_unavailable_message": ai_unavailable_message,
            "signal_explanation": ai_analysis.explain_trade_signal(trade, db),
            "short_address": vh.short_address,
        },
    )


@router.get("/api/trades/{trade_id}/ai-analysis")
@limiter.limit(AI_RATE_LIMIT)
def get_trade_ai_analysis(request: Request, trade_id: str, db: Session = Depends(get_db)):
    """Analyze a trade - local trained model first, external providers as fallback."""
    trade = db.query(Trade).filter(Trade.trade_id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")

    result = ai_analysis.analyze_trade(trade, db)

    if result and result.get("_error") == "model_loading":
        return {
            "trade_id": trade_id,
            "available": False,
            "error": "model_loading",
            "message": "AI model is warming up. Try again in ~20 seconds.",
        }

    available = result is not None and "_error" not in result
    if available:
        unavailable_message = None
    elif settings.ai_provider_configured():
        unavailable_message = settings.ai_unavailable_message()
    else:
        unavailable_message = ai_analysis.local_unavailable_reason(trade, db)
    return {
        "trade_id": trade_id,
        "available": available,
        "reason": None if available else "no_local_score_or_provider",
        "message": unavailable_message,
        "signal": result.get("signal") if result else None,
        "risk": result.get("risk") if result else None,
        "score": result.get("score") if result else None,
        "threshold": result.get("threshold") if result else None,
        "analysis_reason": result.get("reason") if result else None,
        "top_factors": result.get("top_factors") if result else None,
        "price_insight": result.get("price_insight") if result else None,
        "behavior": result.get("behavior") if result else None,
        "verdict": result.get("verdict") if result else None,
        "provider": result.get("provider") if result else None,
        "model_version": result.get("model_version") if result else None,
        "from_cache": result.get("_from_cache", False) if result else False,
        "context": result.get("context") if result else None,
    }


@router.post("/api/trades/{trade_id}/ai-analysis/invalidate")
@limiter.limit(AI_RATE_LIMIT)
def invalidate_trade_ai_analysis(request: Request, trade_id: str, db: Session = Depends(get_db)):
    """Remove the cached AI analysis so it will be re-analyzed on next request."""
    trade = db.query(Trade).filter(Trade.trade_id == trade_id).first()
    if not trade:
        raise HTTPException(status_code=404, detail="Trade not found")
    removed = ai_analysis.invalidate_cache(trade_id, db)
    return {"trade_id": trade_id, "invalidated": removed}
