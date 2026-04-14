import csv
import io
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from app.db import get_db
from app.ingest import cleanup_duplicate_trades, find_duplicate_groups, refresh_wallet
from app.models import SyncEvent, Trade, Wallet
from app.settings import APP_NAME, DEFAULT_PAGE_SIZE, DEFAULT_REFRESH_LIMIT, MAX_PAGE_SIZE

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

WALLET_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _wallet_order_query(db: Session):
    return db.query(Wallet).order_by(desc(func.coalesce(Wallet.is_pinned, 0)), desc(Wallet.created_at))


def _short_address(address: str) -> str:
    if len(address) <= 14:
        return address
    return f"{address[:8]}...{address[-6:]}"


def _flash_redirect(message: str, level: str = "info") -> RedirectResponse:
    return RedirectResponse(url=f"/wallets?flash={quote(message)}&level={quote(level)}", status_code=303)


def _flash_redirect_with_form(
    message: str,
    *,
    level: str = "info",
    address: str = "",
    label: str = "",
) -> RedirectResponse:
    return RedirectResponse(
        url=f"/wallets?flash={quote(message)}&level={quote(level)}&address={quote(address)}&label={quote(label)}",
        status_code=303,
    )


def _validate_wallet_address(address: str) -> Optional[str]:
    candidate = (address or "").strip().lower()
    if not candidate:
        return "Wallet address is required."
    if not WALLET_ADDRESS_RE.match(candidate):
        return "Wallet address must be a valid 42-character hex address starting with 0x."
    return None


def _parse_datetime_start(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text)
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _parse_datetime_end(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text) + timedelta(days=1)
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _pagination_meta(page: int, page_size: int, total_items: int) -> Dict[str, int]:
    if total_items <= 0:
        return {"start": 0, "end": 0}
    start = ((page - 1) * page_size) + 1
    end = min(total_items, page * page_size)
    return {"start": start, "end": end}


def resolve_wallet(db: Session, identifier: str) -> Wallet:
    wallet = None
    if identifier.isdigit():
        wallet = db.query(Wallet).filter(Wallet.id == int(identifier)).first()
    if wallet is None:
        wallet = db.query(Wallet).filter(Wallet.address == identifier.strip().lower()).first()
    if wallet is None:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return wallet


def _wallet_stats_map(db: Session) -> Dict[str, Dict[str, Any]]:
    rows = (
        db.query(
            Trade.wallet_address,
            func.count(Trade.id).label("trade_count"),
            func.max(Trade.traded_at).label("last_trade_at"),
        )
        .group_by(Trade.wallet_address)
        .all()
    )

    stats_map: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        stats_map[row.wallet_address] = {
            "trade_count": int(row.trade_count or 0),
            "last_trade_at": row.last_trade_at,
        }
    return stats_map


def _apply_trade_filters(
    query,
    *,
    wallet_address: Optional[str] = None,
    side: Optional[str] = None,
    market_search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    if wallet_address:
        query = query.filter(Trade.wallet_address == wallet_address)

    if side in {"YES", "NO"}:
        query = query.filter(Trade.side == side)

    if market_search:
        term = f"%{market_search.strip()}%"
        query = query.filter(or_(Trade.market_title.ilike(term), Trade.condition_id.ilike(term)))

    start_at = _parse_datetime_start(date_from)
    if start_at is not None:
        query = query.filter(Trade.traded_at >= start_at)

    end_at = _parse_datetime_end(date_to)
    if end_at is not None:
        query = query.filter(Trade.traded_at < end_at)

    return query


def _sorted_trade_query(query, sort_by: str):
    if sort_by == "time_asc":
        return query.order_by(Trade.traded_at.asc())
    if sort_by == "size_desc":
        return query.order_by(Trade.size.desc(), Trade.traded_at.desc())
    return query.order_by(Trade.traded_at.desc())


@router.get("/")
async def root():
    return RedirectResponse(url="/wallets", status_code=302)


@router.get("/wallets")
async def list_wallets(request: Request, db: Session = Depends(get_db)):
    wallets = _wallet_order_query(db).all()
    wallet_stats = _wallet_stats_map(db)
    recent_events = db.query(SyncEvent).order_by(desc(SyncEvent.created_at)).limit(12).all()
    summary = {
        "wallet_count": len(wallets),
        "trade_count": int(db.query(func.count(Trade.id)).scalar() or 0),
        "refreshed_count": sum(1 for wallet in wallets if wallet.last_checked_at),
        "error_count": sum(1 for wallet in wallets if wallet.last_refresh_status == "error"),
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
            "short_address": _short_address,
        },
    )


@router.post("/wallets")
async def add_wallet(
    db: Session = Depends(get_db),
    address: str = Form(...),
    label: Optional[str] = Form(None),
):
    err = _validate_wallet_address(address)
    normalized_address = address.strip().lower()
    normalized_label = (label or "").strip()

    if err:
        return _flash_redirect_with_form(
            err,
            level="error",
            address=normalized_address,
            label=normalized_label,
        )

    if db.query(Wallet).filter(Wallet.address == normalized_address).first():
        return _flash_redirect_with_form(
            "Wallet already exists in your watchlist.",
            level="error",
            address=normalized_address,
            label=normalized_label,
        )

    wallet = Wallet(address=normalized_address, label=normalized_label or None)
    db.add(wallet)
    db.commit()
    db.refresh(wallet)

    return _flash_redirect("Wallet added. Use Refresh to fetch trades.", "success")


@router.post("/wallets/{identifier}/refresh")
async def refresh_single_wallet(
    identifier: str,
    db: Session = Depends(get_db),
    limit: int = Query(DEFAULT_REFRESH_LIMIT, ge=1, le=1000),
):
    wallet = resolve_wallet(db, identifier)
    result = refresh_wallet(db, wallet, limit=limit)

    if result["status"] == "error":
        return _flash_redirect(f"Refresh failed for {_short_address(wallet.address)}: {result['error']}", "error")
    if result["inserted"] == 0:
        return _flash_redirect(
            f"No new trades for {_short_address(wallet.address)}. Fetched {result['fetched']} records.",
            "info",
        )
    return _flash_redirect(
        (
            f"Refreshed {_short_address(wallet.address)}. "
            f"Added {result['inserted']} new trades from {result['fetched']} fetched records."
        ),
        "success",
    )


@router.post("/wallets/refresh-all")
async def refresh_all_wallets(
    db: Session = Depends(get_db),
    limit: int = Query(DEFAULT_REFRESH_LIMIT, ge=1, le=1000),
):
    wallets = _wallet_order_query(db).all()
    if not wallets:
        return _flash_redirect("No wallets available to refresh.", "info")

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


@router.get("/wallets/{identifier}/delete-confirm")
async def delete_wallet_confirm(request: Request, identifier: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, identifier)
    trade_count = db.query(Trade).filter(Trade.wallet_address == wallet.address).count()
    return templates.TemplateResponse(
        request,
        "wallet_delete_confirm_v2.html",
        {"request": request, "wallet": wallet, "trade_count": trade_count, "short_address": _short_address},
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
    sort_by: str = Query("time_desc"),
    db: Session = Depends(get_db),
):
    wallet = resolve_wallet(db, identifier)

    base_query = _apply_trade_filters(
        db.query(Trade),
        wallet_address=wallet.address,
        side=side,
        market_search=market_search,
        date_from=date_from,
        date_to=date_to,
    )
    sorted_query = _sorted_trade_query(base_query, sort_by)

    total_trades = sorted_query.count()
    total_pages = max(1, (total_trades + page_size - 1) // page_size)
    page = min(page, total_pages)
    pagination = _pagination_meta(page, page_size, total_trades)

    trades = sorted_query.limit(page_size).offset((page - 1) * page_size).all()

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
            "sort_by": sort_by,
            "short_address": _short_address,
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
    wallet_search: Optional[str] = Query(None),
    sort_by: str = Query("time_desc"),
    db: Session = Depends(get_db),
):
    query = _apply_trade_filters(
        db.query(Trade),
        side=side,
        market_search=market_search,
        date_from=date_from,
        date_to=date_to,
    )
    if wallet_search:
        query = query.filter(func.lower(Trade.wallet_address).like(f"%{wallet_search.lower()}%"))

    query = _sorted_trade_query(query, sort_by)
    total_trades = query.count()
    total_pages = max(1, (total_trades + page_size - 1) // page_size)
    page = min(page, total_pages)
    pagination = _pagination_meta(page, page_size, total_trades)

    trades = query.limit(page_size).offset((page - 1) * page_size).all()
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
            "wallet_search": wallet_search,
            "sort_by": sort_by,
            "wallet_map": wallet_map,
            "short_address": _short_address,
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
            "short_address": _short_address,
        },
    )


@router.get("/wallets/{identifier}/trades/export")
async def export_trades(
    identifier: str,
    side: Optional[str] = Query(None),
    market_search: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    sort_by: str = Query("time_desc"),
    db: Session = Depends(get_db),
):
    wallet = resolve_wallet(db, identifier)
    query = _apply_trade_filters(
        db.query(Trade),
        wallet_address=wallet.address,
        side=side,
        market_search=market_search,
        date_from=date_from,
        date_to=date_to,
    )
    query = _sorted_trade_query(query, sort_by)

    output = io.StringIO()
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

    filename = f"trades_{wallet.address[:8]}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/admin/sync-status")
async def sync_status_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "sync_status_v2.html",
        {
            "request": request,
            "app_name": APP_NAME,
            "events": db.query(SyncEvent).order_by(desc(SyncEvent.created_at)).limit(100).all(),
            "duplicates": find_duplicate_groups(db),
        },
    )


@router.post("/admin/sync-status/cleanup")
async def cleanup_sync_duplicates(db: Session = Depends(get_db)):
    return JSONResponse({"status": "success", "removed": cleanup_duplicate_trades(db)})


@router.post("/admin/refresh")
async def refresh_trades(
    address: Optional[str] = Query(None),
    limit_per_wallet: int = Query(DEFAULT_REFRESH_LIMIT, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    if address:
        wallet = resolve_wallet(db, address)
        return JSONResponse({"status": "success", **refresh_wallet(db, wallet, limit=limit_per_wallet)})

    results: Dict[str, Any] = {}
    for wallet in _wallet_order_query(db).all():
        results[wallet.address] = refresh_wallet(db, wallet, limit=limit_per_wallet)
    return JSONResponse({"status": "success", "wallets_refreshed": len(results), "results": results})


@router.post("/admin/refresh-all")
async def refresh_all_trades(
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
    for wallet in _wallet_order_query(db).all():
        results[wallet.address] = refresh_wallet(db, wallet, fetch_all=True, limit=limit_per_wallet)
    return JSONResponse(
        {
            "status": "success",
            "wallets_refreshed": len(results),
            "results": results,
            "message": "Full history fetch complete for all wallets",
        }
    )
