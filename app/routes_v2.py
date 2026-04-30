import csv
import io
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.db import get_db
from app.ingest import cleanup_duplicate_trades, find_duplicate_groups, refresh_wallet
from app.models import SyncEvent, Trade, Wallet
from app.settings import APP_NAME, DEFAULT_PAGE_SIZE, DEFAULT_REFRESH_LIMIT, MAX_PAGE_SIZE
from app import view_helpers as vh

_BOM = "\ufeff"

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

def _flash_redirect(message: str, level: str = "info") -> RedirectResponse:
    return RedirectResponse(url=f"/wallets?flash={quote(message)}&level={quote(level)}", status_code=303)


def _flash_redirect_to(url: str, message: str, level: str = "info") -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url=f"{url}{sep}flash={quote(message)}&level={quote(level)}", status_code=303)


def _safe_next(next_path: Optional[str]) -> Optional[str]:
    """Return next_path only if it is an internal safe path, otherwise None."""
    if not next_path:
        return None
    if next_path == "/all-trades" or next_path.startswith("/wallets/"):
        return next_path
    return None


def _flash_redirect_with_form(
    message: str,
    *,
    level: str = "info",
    address: str = "",
    label: str = "",
    tags: str = "",
    notes: str = "",
) -> RedirectResponse:
    return RedirectResponse(
        url=(
            f"/wallets?flash={quote(message)}&level={quote(level)}&address={quote(address)}"
            f"&label={quote(label)}&tags={quote(tags)}&notes={quote(notes)}"
        ),
        status_code=303,
    )


def resolve_wallet(db: Session, identifier: str) -> Wallet:
    wallet = None
    if identifier.isdigit():
        wallet = db.query(Wallet).filter(Wallet.id == int(identifier)).first()
    if wallet is None:
        wallet = db.query(Wallet).filter(Wallet.address == identifier.strip().lower()).first()
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return wallet


@router.get("/")
async def root():
    return RedirectResponse(url="/wallets", status_code=302)


@router.get("/dashboard")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    total_wallets = db.query(func.count(Wallet.id)).scalar() or 0
    active_wallets_count = db.query(func.count(Wallet.id)).filter(func.coalesce(Wallet.is_archived, 0) == 0).scalar() or 0
    archived_wallets_count = db.query(func.count(Wallet.id)).filter(func.coalesce(Wallet.is_archived, 0) == 1).scalar() or 0

    total_trades = db.query(func.count(Trade.id)).scalar() or 0

    last_success_at = db.query(func.max(SyncEvent.created_at)).filter(SyncEvent.status == "success").scalar()
    last_error_at = db.query(func.max(SyncEvent.created_at)).filter(SyncEvent.status == "error").scalar()

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


@router.get("/wallets/export")
async def export_wallets(db: Session = Depends(get_db)):
    wallets = db.query(Wallet).order_by(Wallet.created_at).all()

    def _rows():
        yield _BOM
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["address", "label", "tags", "notes", "is_pinned", "is_archived", "created_at"])
        yield buf.getvalue()
        for w in wallets:
            buf = io.StringIO()
            writer = csv.writer(buf)
            tags_str = ";".join(vh.tag_list(w.tags)) if w.tags else ""
            writer.writerow([
                w.address,
                w.label or "",
                tags_str,
                w.notes or "",
                "1" if w.is_pinned else "0",
                "1" if w.is_archived else "0",
                w.created_at.strftime("%Y-%m-%d %H:%M:%S") if w.created_at else "",
            ])
            yield buf.getvalue()

    filename = quote("wallets_export.csv")
    return StreamingResponse(
        _rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


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
    total = 0
    added = 0
    duplicates = 0
    invalid = 0

    for row in reader:
        total += 1
        raw_address = (row.get("address") or "").strip().lower()
        if vh.validate_wallet_address(raw_address):
            invalid += 1
            continue
        if db.query(Wallet).filter(Wallet.address == raw_address).first():
            duplicates += 1
            continue

        raw_tags = (row.get("tags") or "").strip()
        tags_normalized = vh.normalize_tags(raw_tags.replace(";", ",")) if raw_tags else None

        def _parse_bool(val: str) -> int:
            return 1 if val.strip().lower() in ("1", "true") else 0

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
                "invalid": invalid,
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
    wallets = vh.build_wallet_query(
        db,
        wallet_search=wallet_search,
        status_filter=status_filter,
        include_archived=bool(include_archived),
    ).all()
    wallet_stats = vh.wallet_stats_map(db, [wallet.address for wallet in wallets])
    recent_events = db.query(SyncEvent).order_by(desc(SyncEvent.created_at)).limit(12).all()
    summary = vh.wallet_summary_counts(
        db,
        wallet_search=wallet_search,
        status_filter=status_filter,
        include_archived=bool(include_archived),
    )
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


@router.get("/wallets/{identifier}")
async def wallet_detail(request: Request, identifier: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, identifier)

    summary_row = db.query(
        func.count(Trade.id).label("total_trades"),
        func.min(Trade.traded_at).label("first_trade_at"),
        func.max(Trade.traded_at).label("latest_trade_at"),
    ).filter(Trade.wallet_address == wallet.address).first()

    trade_query = db.query(Trade).filter(Trade.wallet_address == wallet.address)
    pnl = vh.trade_pnl_summary(trade_query)
    activity_timeline = vh.build_wallet_activity_timeline(db, wallet.address, limit=30)
    wallet_intelligence = vh.get_wallet_intelligence_summary(db, wallet.address)

    return templates.TemplateResponse(
        request,
        "wallet_detail_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "wallet": wallet,
            "summary_row": summary_row,
            "pnl": pnl,
            "activity_timeline": activity_timeline,
            "wallet_intelligence": wallet_intelligence,
            "short_address": vh.short_address,
            "duration_label": vh.duration_label,
            "sync_status_class": vh.sync_status_class,
            "wallet_freshness_label": vh.wallet_freshness_label,
            "wallet_status_tone": vh.wallet_status_tone,
        },
    )


@router.post("/wallets/{identifier}/refresh")
def refresh_single_wallet(
    identifier: str,
    db: Session = Depends(get_db),
    limit: int = Query(DEFAULT_REFRESH_LIMIT, ge=1, le=1000),
    next_path: Optional[str] = Query(None, alias="next"),
):
    wallet = resolve_wallet(db, identifier)
    result = refresh_wallet(db, wallet, limit=limit)
    redirect_to = _safe_next(next_path)

    if result["status"] == "error":
        msg = f"Refresh failed for {vh.short_address(wallet.address)}: {result['error']}"
        return _flash_redirect_to(redirect_to, msg, "error") if redirect_to else _flash_redirect(msg, "error")
    if result["inserted"] == 0:
        msg = f"No new trades for {vh.short_address(wallet.address)}. Fetched {result['fetched']} records."
        return _flash_redirect_to(redirect_to, msg, "info") if redirect_to else _flash_redirect(msg, "info")
    msg = (
        f"Refreshed {vh.short_address(wallet.address)}. "
        f"Added {result['inserted']} new trades from {result['fetched']} fetched records."
    )
    return _flash_redirect_to(redirect_to, msg, "success") if redirect_to else _flash_redirect(msg, "success")


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
async def toggle_wallet_pin(identifier: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, identifier)
    wallet.is_pinned = 0 if wallet.is_pinned else 1
    db.commit()
    state = "Pinned" if wallet.is_pinned else "Unpinned"
    return _flash_redirect(f"{state} {vh.short_address(wallet.address)}.", "success")


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
    summary_row = query.with_entities(
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


@router.get("/all-trades/export")
async def export_all_trades(
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

    wallet_map = {w.address: w for w in db.query(Wallet).all()}

    output = io.StringIO()
    output.write(_BOM)
    writer = csv.writer(output)
    writer.writerow(["Trade ID", "Date (UTC)", "Wallet", "Market Title", "Condition ID", "Side", "Price", "Size", "Value"])
    for trade in query.all():
        w = wallet_map.get(trade.wallet_address)
        wallet_label = w.label if w and w.label else trade.wallet_address
        writer.writerow([
            trade.trade_id,
            trade.traded_at.strftime("%Y-%m-%d %H:%M:%S"),
            wallet_label,
            trade.market_title or "N/A",
            trade.condition_id,
            trade.side,
            f"{trade.price:.4f}",
            f"{trade.size:.2f}",
            f"{(trade.price * trade.size):.2f}",
        ])

    filename = f"all_trades_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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


@router.get("/wallets/{identifier}/trades/export")
async def export_trades(
    identifier: str,
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
    query = vh.apply_trade_filters(
        db.query(Trade),
        wallet_address=wallet.address,
        side=side,
        market_search=market_search,
        date_from=date_from,
        date_to=date_to,
    )
    query = vh.sorted_trade_query(query, sort_by)

    output = io.StringIO()
    output.write(_BOM)
    writer = csv.writer(output)
    writer.writerow(["Trade ID", "Date (UTC)", "Market Title", "Condition ID", "Side", "Price", "Size", "Value"])
    for trade in query.all():
        writer.writerow(
            [
                trade.trade_id,
                trade.traded_at.strftime("%Y-%m-%d %H:%M:%S"),
                trade.market_title or "N/A",
                trade.condition_id,
                trade.side,
                f"{trade.price:.4f}",
                f"{trade.size:.2f}",
                f"{(trade.price * trade.size):.2f}",
            ]
        )

    filename = f"trades_{wallet.address[:8]}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/admin/sync-status")
async def sync_status_page(
    request: Request,
    wallet_search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    error_only: int = Query(0),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    events_query = vh.filter_sync_events(
        db.query(SyncEvent).order_by(desc(SyncEvent.created_at)),
        wallet_search=wallet_search,
        status=status,
        error_only=bool(error_only),
    )
    total_events = events_query.count()
    total_pages = max(1, (total_events + page_size - 1) // page_size)
    page = min(page, total_pages)
    events = events_query.limit(page_size).offset((page - 1) * page_size).all()
    pagination = vh.pagination_meta(page, page_size, total_events)
    duplicates = find_duplicate_groups(
        db,
        wallet_search.lower() if wallet_search and vh.WALLET_ADDRESS_RE.match(wallet_search.lower()) else None,
    )
    return templates.TemplateResponse(
        request,
        "sync_status_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "events": events,
            "total_events": total_events,
            "total_pages": total_pages,
            "page": page,
            "page_size": page_size,
            "pagination": pagination,
            "duplicates": duplicates,
            "wallet_search": wallet_search,
            "status_filter": status,
            "error_only": bool(error_only),
            "sync_status_class": vh.sync_status_class,
            "duration_label": vh.duration_label,
            "short_address": vh.short_address,
            "flash": request.query_params.get("flash"),
            "flash_level": request.query_params.get("level", "info"),
        },
    )


@router.post("/admin/sync-status/cleanup")
def cleanup_sync_duplicates(db: Session = Depends(get_db)):
    removed = cleanup_duplicate_trades(db)
    msg = f"Removed {removed} duplicate trade{'s' if removed != 1 else ''}." if removed else "No duplicate trades found."
    level = "success" if removed else "info"
    return _flash_redirect_to("/admin/sync-status", msg, level)


@router.post("/admin/refresh")
def refresh_trades(
    address: Optional[str] = Query(None),
    limit_per_wallet: int = Query(DEFAULT_REFRESH_LIMIT, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    if address:
        wallet = resolve_wallet(db, address)
        return JSONResponse({"status": "success", **refresh_wallet(db, wallet, limit=limit_per_wallet)})

    results: Dict[str, Any] = {}
    for wallet in vh.active_wallets(vh.wallet_order_query(db).all()):
        results[wallet.address] = refresh_wallet(db, wallet, limit=limit_per_wallet)
    return JSONResponse({"status": "success", "wallets_refreshed": len(results), "results": results})


@router.post("/admin/refresh-all")
def refresh_all_trades(
    address: Optional[str] = Query(None),
    limit_per_wallet: int = Query(DEFAULT_REFRESH_LIMIT, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    if address:
        wallet = resolve_wallet(db, address)
        return JSONResponse(
            {
                "status": "success",
                "message": "Full history fetch complete",
                **refresh_wallet(db, wallet, fetch_all=True, limit=limit_per_wallet),
            }
        )

    results: Dict[str, Any] = {}
    for wallet in vh.active_wallets(vh.wallet_order_query(db).all()):
        results[wallet.address] = refresh_wallet(db, wallet, fetch_all=True, limit=limit_per_wallet)
    return JSONResponse(
        {
            "status": "success",
            "wallets_refreshed": len(results),
            "results": results,
            "message": "Full history fetch complete for all wallets",
        }
    )
