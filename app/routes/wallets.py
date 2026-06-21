# Handles wallet pages and wallet actions.
"""Wallet management routes: list, add, edit, pin, archive, delete, import."""
import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from sqlalchemy import case, desc, func
from sqlalchemy.orm import Session

from app import ai_analysis, alerts, settings
from app import retention as ret
from app import view_helpers as vh
from app.db import get_db
from app.ingest import refresh_wallet
from app.ml.model import effective_signal_threshold, get_signal_model
from app.models import SyncEvent, Trade, Wallet
from app.routes._shared import (
    _flash_redirect,
    _flash_redirect_to,
    _flash_redirect_with_form,
    _safe_next,
    resolve_wallet,
    sanitize_search,
    templates,
)
from app.settings import APP_NAME, DEFAULT_REFRESH_LIMIT, RETENTION_METRICS_ENABLED

router = APIRouter()


@router.get("/wallets/import")
async def import_wallets_form(request: Request):
    return templates.TemplateResponse(
        request,
        "wallets_import.html",
        {"request": request, "app_name": APP_NAME},
    )


@router.post("/wallets/import")
async def import_wallets(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "address" not in reader.fieldnames:
        return templates.TemplateResponse(
            request,
            "wallets_import.html",
            {
                "request": request,
                "app_name": APP_NAME,
                "error": 'Missing required "address" column. Expected columns: address, label, tags, notes, is_pinned, is_archived.',
                "result": None,
            },
        )
    total = 0
    added = 0
    duplicates = 0
    skipped_reasons: list = []

    def _parse_bool(val: str) -> int:
        return 1 if val.strip().lower() in ("1", "true") else 0

    for row in reader:
        total += 1
        raw_address = (row.get("address") or "").strip().lower()
        validation_error = vh.validate_wallet_address(raw_address)
        if validation_error:
            skipped_reasons.append(f"Row {total}: {validation_error} (value: {raw_address!r})")
            continue
        if db.query(Wallet).filter(Wallet.address == raw_address).first():
            duplicates += 1
            continue

        raw_tags = (row.get("tags") or "").strip()
        tags_normalized = vh.normalize_tags(raw_tags.replace(";", ",")) if raw_tags else None

        wallet = Wallet(
            address=raw_address,
            label=(row.get("label") or "").strip() or None,
            tags=tags_normalized or None,
            notes=(row.get("notes") or "").strip() or None,
            is_pinned=_parse_bool(row.get("is_pinned") or "0"),
            is_archived=_parse_bool(row.get("is_archived") or "0"),
        )
        db.add(wallet)
        added += 1

    db.commit()

    return templates.TemplateResponse(
        request,
        "wallets_import.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "result": {
                "total": total,
                "added": added,
                "duplicates": duplicates,
                "invalid": len(skipped_reasons),
                "skipped_reasons": skipped_reasons,
            },
        },
    )


@router.get("/wallets")
async def list_wallets(
    request: Request,
    wallet_search: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None),
    include_archived: int = Query(0),
    db: Session = Depends(get_db),
):
    if RETENTION_METRICS_ENABLED:
        ret.emit(ret.RawEvent(
            tracker_id=ret.get_or_create_tracker_id(request),
            event_name="page_view",
            route="wallets",
        ))

    wallet_search = sanitize_search(wallet_search)
    wallets = vh.build_wallet_query(
        db,
        wallet_search=wallet_search,
        status_filter=status_filter,
        include_archived=bool(include_archived),
    ).all()
    wallet_stats = vh.wallet_stats_map(db, [wallet.address for wallet in wallets])
    recent_events = db.query(SyncEvent).order_by(desc(SyncEvent.created_at)).limit(12).all()
    summary = {
        "wallet_count": len(wallets),
        "pinned_count": sum(1 for w in wallets if bool(w.is_pinned)),
        "archived_count": sum(1 for w in wallets if bool(w.is_archived)),
        "refreshed_count": sum(1 for w in wallets if w.last_checked_at is not None),
        "error_count": sum(1 for w in wallets if w.last_refresh_status == "error"),
        "trade_count": sum(int((wallet_stats.get(w.address) or {}).get("trade_count") or 0) for w in wallets),
    }
    return templates.TemplateResponse(
        request,
        "wallets_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "wallets": wallets,
            "wallet_stats": wallet_stats,
            "recent_events": recent_events,
            "summary": summary,
            "flash": request.query_params.get("flash"),
            "flash_level": request.query_params.get("level", "info"),
            "form_address": request.query_params.get("address", ""),
            "form_label": request.query_params.get("label", ""),
            "form_tags": request.query_params.get("tags", ""),
            "form_notes": request.query_params.get("notes", ""),
            "wallet_search": wallet_search,
            "status_filter": status_filter,
            "include_archived": bool(include_archived),
            "short_address": vh.short_address,
            "tag_list": vh.tag_list,
            "wallet_freshness_label": vh.wallet_freshness_label,
            "wallet_status_tone": vh.wallet_status_tone,
        },
    )


@router.post("/wallets")
async def add_wallet(
    db: Session = Depends(get_db),
    address: str = Form(...),
    label: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
):
    err = vh.validate_wallet_address(address)
    normalized_address = address.strip().lower()
    normalized_label = (label or "").strip()
    normalized_tags = vh.normalize_tags(tags)
    normalized_notes = (notes or "").strip()

    if err:
        return _flash_redirect_with_form(
            err,
            level="error",
            address=normalized_address,
            label=normalized_label,
            tags=normalized_tags,
            notes=normalized_notes,
        )

    if db.query(Wallet).filter(Wallet.address == normalized_address).first():
        return _flash_redirect_with_form(
            "Wallet already exists in your watchlist.",
            level="error",
            address=normalized_address,
            label=normalized_label,
            tags=normalized_tags,
            notes=normalized_notes,
        )

    wallet = Wallet(
        address=normalized_address,
        label=normalized_label or None,
        tags=normalized_tags or None,
        notes=normalized_notes or None,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)

    return _flash_redirect("Wallet added. Use Refresh to fetch trades.", "success")


@router.post("/wallets/refresh-all")
def refresh_all_wallets(
    db: Session = Depends(get_db),
    limit: int = Query(DEFAULT_REFRESH_LIMIT, ge=1, le=1000),
):
    wallets = vh.active_wallets(vh.wallet_order_query(db).all())
    if not wallets:
        return _flash_redirect("No active wallets available to refresh.", "info")

    total_inserted = 0
    total_fetched = 0
    failures = 0
    no_new = 0
    for wallet in wallets:
        result = refresh_wallet(db, wallet, limit=limit)
        total_inserted += int(result["inserted"])
        total_fetched += int(result["fetched"])
        if result["status"] == "error":
            failures += 1
        elif result["status"] == "no_new":
            no_new += 1
        alerts.fire_alerts_for_new_trades(db, wallet)

    if failures:
        return _flash_redirect(
            f"Refresh all finished with {failures} failures. Fetched {total_fetched} records and added {total_inserted} trades.",
            "warning",
        )
    if total_inserted == 0:
        return _flash_redirect(
            f"Refresh all completed. No new trades found across {len(wallets)} wallets after fetching {total_fetched} records.",
            "info",
        )
    return _flash_redirect(
        (
            f"Refresh all completed. Added {total_inserted} new trades from {total_fetched} fetched records. "
            f"{no_new} wallets had no new activity."
        ),
        "success",
    )


@router.get("/leaderboard")
async def leaderboard(request: Request, db: Session = Depends(get_db)):
    """Rank tracked wallets by total stored trade value.

    Resolved-performance columns (ROI, PnL, wallet score) are placeholders until
    market-outcome data is stored; this keeps the leaderboard honest, not faked.
    """
    if RETENTION_METRICS_ENABLED:
        ret.emit(ret.RawEvent(
            tracker_id=ret.get_or_create_tracker_id(request),
            event_name="page_view",
            route="leaderboard",
        ))

    trade_value = Trade.price * Trade.size
    ranked = (
        db.query(
            Trade.wallet_address,
            func.count(Trade.id).label("trade_count"),
            func.sum(trade_value).label("total_value"),
        )
        .group_by(Trade.wallet_address)
        .order_by(func.sum(trade_value).desc())
        .limit(100)
        .all()
    )
    addresses = [r.wallet_address for r in ranked]
    wallet_map = {
        w.address: w
        for w in db.query(Wallet).filter(Wallet.address.in_(addresses)).all()
    } if addresses else {}

    rows = []
    for position, row in enumerate(ranked, start=1):
        wallet = wallet_map.get(row.wallet_address)
        tags = vh.tag_list(wallet.tags) if wallet and wallet.tags else []
        rows.append({
            "rank": position,
            "address": row.wallet_address,
            "label": wallet.label if wallet and wallet.label else None,
            "focus": tags[0] if tags else None,
            "trade_count": int(row.trade_count or 0),
            "total_value": float(row.total_value or 0),
        })

    return templates.TemplateResponse(
        request,
        "leaderboard_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "rows": rows,
            "short_address": vh.short_address,
        },
    )


@router.get("/wallets/{identifier}")
async def wallet_detail(request: Request, identifier: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, identifier)

    recent_cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    summary_row = db.query(
        func.count(Trade.id).label("total_trades"),
        func.min(Trade.traded_at).label("first_trade_at"),
        func.max(Trade.traded_at).label("latest_trade_at"),
        func.sum(case(
            (Trade.traded_at >= recent_cutoff, Trade.price * Trade.size), else_=0
        )).label("recent_value_24h"),
    ).filter(Trade.wallet_address == wallet.address).first()

    trade_query = db.query(Trade).filter(Trade.wallet_address == wallet.address)
    pnl = vh.trade_pnl_summary(trade_query)
    activity_timeline = vh.build_wallet_activity_timeline(db, wallet.address, limit=30)
    wallet_intelligence = vh.get_wallet_intelligence_summary(db, wallet.address)
    wallet_top_markets = vh.build_top_markets(db, wallet_address=wallet.address)
    wallet_activity_days = vh.build_activity_heatmap(db, wallet_address=wallet.address)

    flagged_query = (
        db.query(Trade)
        .filter(Trade.wallet_address == wallet.address)
        .filter(Trade.notable_score >= effective_signal_threshold())
    )
    signal_flagged_count = flagged_query.count()
    signal_recent_flagged = flagged_query.order_by(Trade.traded_at.desc()).limit(5).all()

    total_value = float(pnl["total_value"] or 0)
    yes_value = float(pnl["yes_value"] or 0)
    no_value = float(pnl["no_value"] or 0)
    yes_value_pct = round((yes_value / total_value) * 100) if total_value else 0
    no_value_pct = 100 - yes_value_pct if total_value else 0

    recent_value_24h = float(summary_row.recent_value_24h or 0)

    trade_count = int(pnl["trade_count"] or 0)
    avg_trade_value = total_value / trade_count if trade_count else 0.0

    # Biggest single trade by notional value (reuses the wallet-scoped query).
    biggest_trade = trade_query.order_by((Trade.price * Trade.size).desc()).first()
    biggest_trade_value = float(biggest_trade.price * biggest_trade.size) if biggest_trade else 0.0

    # Rank this wallet against every other wallet by total stored trade value.
    value_rows = (
        db.query(func.sum(Trade.price * Trade.size).label("v"))
        .group_by(Trade.wallet_address)
        .all()
    )
    peer_values = [float(r.v or 0) for r in value_rows]
    ranked_total = sum(1 for v in peer_values if v > 0)
    wallet_rank = (1 + sum(1 for v in peer_values if v > total_value)) if total_value > 0 else None

    # Lightweight, data-derived persona (no resolved-market PnL required).
    trades_last_24h = int(wallet_intelligence.get("trades_last_24h") or 0)
    if trade_count == 0:
        wallet_persona = "Unseen wallet"
    elif avg_trade_value >= 10000:
        wallet_persona = "Whale"
    elif avg_trade_value >= 1000:
        wallet_persona = "High-volume trader"
    elif trades_last_24h >= 10:
        wallet_persona = "Active trader"
    else:
        wallet_persona = "Retail trader"

    wallet_insights = [
        {
            "label": "24h value",
            "value": f"${recent_value_24h:,.2f}",
            "detail": "Stored trade value in the last day",
            "tone": "success" if recent_value_24h else "info",
        },
        {
            "label": "YES / NO trades",
            "value": f"{pnl['yes_count']} / {pnl['no_count']}",
            "detail": "Trade count split across stored history",
            "tone": "info",
        },
        {
            "label": "Top market",
            "value": wallet_top_markets[0]["market"] if wallet_top_markets else "None yet",
            "detail": f"${wallet_top_markets[0]['total_value']:,.2f} stored value" if wallet_top_markets else "Refresh this wallet to populate markets",
            "tone": "success" if wallet_top_markets else "info",
        },
    ]

    return templates.TemplateResponse(
        request,
        "wallet_detail_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "flash": request.query_params.get("flash"),
            "flash_level": request.query_params.get("level", "info"),
            "wallet": wallet,
            "summary_row": summary_row,
            "pnl": pnl,
            "yes_value_pct": yes_value_pct,
            "no_value_pct": no_value_pct,
            "recent_value_24h": recent_value_24h,
            "avg_trade_value": avg_trade_value,
            "biggest_trade": biggest_trade,
            "biggest_trade_value": biggest_trade_value,
            "wallet_rank": wallet_rank,
            "ranked_total": ranked_total,
            "wallet_persona": wallet_persona,
            "is_following": bool(wallet.is_pinned),
            "wallet_insights": wallet_insights,
            "wallet_top_markets": wallet_top_markets,
            "wallet_activity_days": wallet_activity_days,
            "activity_timeline": activity_timeline,
            "wallet_intelligence": wallet_intelligence,
            "signal_flagged_count": signal_flagged_count,
            "signal_recent_flagged": signal_recent_flagged,
            "signal_model_loaded": get_signal_model() is not None,
            "short_address": vh.short_address,
            "duration_label": vh.duration_label,
            "sync_status_class": vh.sync_status_class,
            "wallet_freshness_label": vh.wallet_freshness_label,
            "wallet_status_tone": vh.wallet_status_tone,
            "ai_provider_configured": settings.ai_provider_configured(),
            "ai_unavailable_message": settings.ai_unavailable_message(),
        },
    )


@router.post("/wallets/{identifier}/refresh")
def refresh_single_wallet(
    request: Request,
    identifier: str,
    db: Session = Depends(get_db),
    limit: int = Query(DEFAULT_REFRESH_LIMIT, ge=1, le=1000),
    next_path: Optional[str] = Query(None, alias="next"),
):
    if RETENTION_METRICS_ENABLED:
        ret.emit(ret.RawEvent(
            tracker_id=ret.get_or_create_tracker_id(request),
            event_name="refresh_triggered",
            route="wallet_refresh",
            metadata={"identifier": identifier},
        ))

    wallet = resolve_wallet(db, identifier)
    result = refresh_wallet(db, wallet, limit=limit)
    redirect_to = _safe_next(next_path)

    if result["status"] == "error":
        msg = f"Refresh failed for {vh.short_address(wallet.address)}: {result['error']}"
        return _flash_redirect_to(redirect_to, msg, "error") if redirect_to else _flash_redirect(msg, "error")

    alerts.fire_alerts_for_new_trades(db, wallet)

    if result["inserted"] == 0:
        msg = f"No new trades for {vh.short_address(wallet.address)}. Fetched {result['fetched']} records."
        return _flash_redirect_to(redirect_to, msg, "info") if redirect_to else _flash_redirect(msg, "info")
    msg = (
        f"Refreshed {vh.short_address(wallet.address)}. "
        f"Added {result['inserted']} new trades from {result['fetched']} fetched records."
    )
    return _flash_redirect_to(redirect_to, msg, "success") if redirect_to else _flash_redirect(msg, "success")


@router.get("/wallets/{identifier}/edit")
async def edit_wallet_page(request: Request, identifier: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, identifier)
    trade_count = db.query(Trade).filter(Trade.wallet_address == wallet.address).count()
    return templates.TemplateResponse(
        request,
        "wallet_edit_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "wallet": wallet,
            "trade_count": trade_count,
            "short_address": vh.short_address,
        },
    )


@router.post("/wallets/{identifier}/edit")
async def edit_wallet(
    identifier: str,
    label: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    is_pinned: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    wallet = resolve_wallet(db, identifier)
    wallet.label = (label or "").strip() or None
    wallet.tags = vh.normalize_tags(tags) or None
    wallet.notes = (notes or "").strip() or None
    wallet.is_pinned = 1 if is_pinned else 0
    db.commit()
    return _flash_redirect(f"Updated {vh.short_address(wallet.address)}.", "success")


@router.post("/wallets/{identifier}/pin")
async def toggle_wallet_pin(
    identifier: str,
    db: Session = Depends(get_db),
    next_path: Optional[str] = Query(None, alias="next"),
):
    wallet = resolve_wallet(db, identifier)
    wallet.is_pinned = 0 if wallet.is_pinned else 1
    db.commit()
    state = "Following" if wallet.is_pinned else "Unfollowed"
    msg = f"{state} {vh.short_address(wallet.address)}."
    redirect_to = _safe_next(next_path)
    return _flash_redirect_to(redirect_to, msg, "success") if redirect_to else _flash_redirect(msg, "success")


@router.post("/wallets/{identifier}/archive")
async def archive_wallet(identifier: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, identifier)
    wallet.is_archived = 1
    db.commit()
    return _flash_redirect(f"Archived {vh.short_address(wallet.address)}. It is now hidden from the default watchlist.", "success")


@router.post("/wallets/{identifier}/unarchive")
async def unarchive_wallet(identifier: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, identifier)
    wallet.is_archived = 0
    db.commit()
    return _flash_redirect(f"Restored {vh.short_address(wallet.address)} to the active watchlist.", "success")


@router.get("/wallets/{identifier}/delete-confirm")
async def delete_wallet_confirm(request: Request, identifier: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, identifier)
    trade_count = db.query(Trade).filter(Trade.wallet_address == wallet.address).count()
    return templates.TemplateResponse(
        request,
        "wallet_delete_confirm_v2.html",
        {"request": request, "wallet": wallet, "trade_count": trade_count, "short_address": vh.short_address},
    )


@router.post("/wallets/{identifier}/delete")
async def delete_wallet(
    identifier: str,
    confirm_text: str = Form(""),
    db: Session = Depends(get_db),
):
    wallet = resolve_wallet(db, identifier)
    if confirm_text.strip().upper() != "DELETE":
        return _flash_redirect("Deletion cancelled. Type DELETE to confirm wallet removal.", "warning")

    db.query(Trade).filter(Trade.wallet_address == wallet.address).delete()
    db.delete(wallet)
    db.commit()
    return _flash_redirect("Wallet and associated trades were deleted.", "success")


@router.get("/api/wallets/{identifier}/ai-summary")
def get_wallet_ai_summary(identifier: str, db: Session = Depends(get_db)):
    """Generate an AI-powered text summary of a wallet's recent trading pattern."""
    wallet = resolve_wallet(db, identifier)
    recent_trades = (
        db.query(Trade)
        .filter(Trade.wallet_address == wallet.address)
        .order_by(Trade.traded_at.desc())
        .limit(15)
        .all()
    )
    if not recent_trades:
        return {"available": False, "summary": None, "reason": "no_trades"}

    summary = ai_analysis.get_trade_summary(recent_trades, db)
    if summary is None:
        return {
            "available": False,
            "summary": None,
            "reason": "no_provider",
            "message": settings.ai_unavailable_message(),
        }

    return {"available": True, "summary": summary, "trade_count": len(recent_trades)}
