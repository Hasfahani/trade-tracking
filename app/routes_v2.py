import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import Session

from app.db import get_db
from app.ingest import (
    calculate_wallet_stats_snapshot,
    cleanup_duplicate_trades,
    find_duplicate_groups,
    get_notification_settings,
    refresh_wallet,
)
from app.models import Notification, SyncEvent, Trade, Wallet
from app.settings import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _wallet_order_query(db: Session):
    return db.query(Wallet).order_by(
        desc(func.coalesce(Wallet.is_pinned, 0)),
        desc(Wallet.created_at),
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


def serialize_trade(trade: Trade) -> Dict[str, Any]:
    return {
        "id": trade.id,
        "trade_id": trade.trade_id,
        "wallet_address": trade.wallet_address,
        "market_title": trade.market_title or "Unknown Market",
        "condition_id": trade.condition_id,
        "side": trade.side,
        "price": trade.price,
        "size": trade.size,
        "value": round(trade.price * trade.size, 2),
        "traded_at": trade.traded_at.isoformat() if trade.traded_at else None,
    }


def calculate_wallet_stats(db: Session, wallet_address: str) -> Dict[str, Any]:
    stats = calculate_wallet_stats_snapshot(db, wallet_address)
    if stats["last_trade_date"]:
        stats["last_trade_date"] = datetime.fromisoformat(stats["last_trade_date"])
    return stats


def build_dashboard_stats(db: Session) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    total_wallets = db.query(Wallet).count()
    total_trades = db.query(Trade).count()
    trades_today = db.query(Trade).filter(Trade.traded_at >= today_start).count()
    most_active_wallet_row = (
        db.query(Trade.wallet_address, func.count(Trade.id).label("trade_count"))
        .group_by(Trade.wallet_address)
        .order_by(desc("trade_count"))
        .first()
    )
    biggest_trade = db.query(Trade).order_by(desc(Trade.price * Trade.size), desc(Trade.traded_at)).first()
    recent_alerts = db.query(Notification).order_by(desc(Notification.created_at)).limit(5).all()
    return {
        "total_wallets": total_wallets,
        "total_trades": total_trades,
        "trades_today": trades_today,
        "recent_trades_24h": db.query(Trade).filter(Trade.traded_at >= (now - timedelta(days=1))).count(),
        "most_active_wallet": most_active_wallet_row.wallet_address if most_active_wallet_row else None,
        "most_active_wallet_count": most_active_wallet_row.trade_count if most_active_wallet_row else 0,
        "biggest_trade": biggest_trade,
        "recent_alerts": recent_alerts,
    }


def calculate_pnl_analytics(db: Session, wallet_address: str) -> Dict[str, Any]:
    trades: List[Trade] = (
        db.query(Trade)
        .filter(Trade.wallet_address == wallet_address)
        .order_by(Trade.traded_at)
        .all()
    )
    markets: Dict[str, Dict[str, Any]] = {}
    for trade in trades:
        market = markets.setdefault(
            trade.condition_id,
            {
                "condition_id": trade.condition_id,
                "market_title": trade.market_title or "Unknown Market",
                "buys": [],
                "sells": [],
            },
        )
        if trade.market_title:
            market["market_title"] = trade.market_title
        if trade.side == "YES":
            market["buys"].append(trade)
        else:
            market["sells"].append(trade)

    closed_positions: List[Dict[str, Any]] = []
    open_positions: List[Dict[str, Any]] = []
    for cid, market in markets.items():
        buys = market["buys"]
        sells = market["sells"]
        total_buy_shares = sum(t.size for t in buys)
        total_buy_cost = sum(t.price * t.size for t in buys)
        total_sell_shares = sum(t.size for t in sells)
        total_sell_proceeds = sum(t.price * t.size for t in sells)
        avg_buy_price = (total_buy_cost / total_buy_shares) if total_buy_shares > 0 else 0.0
        closed_shares = min(total_buy_shares, total_sell_shares)
        net_shares = total_buy_shares - total_sell_shares
        opened_at = buys[0].traded_at if buys else None
        closed_at = sells[-1].traded_at if sells else None

        if closed_shares > 0.0001:
            realized_pnl = total_sell_proceeds - (closed_shares * avg_buy_price)
            amount_bet = closed_shares * avg_buy_price
            roi_pct = (realized_pnl / amount_bet * 100) if amount_bet > 0 else 0.0
            result = "WIN" if realized_pnl > 0.005 else "LOSS" if realized_pnl < -0.005 else "BREAKEVEN"
            closed_positions.append(
                {
                    "condition_id": cid,
                    "market_title": market["market_title"],
                    "buy_shares": round(total_buy_shares, 4),
                    "sell_shares": round(total_sell_shares, 4),
                    "amount_bet": round(amount_bet, 2),
                    "proceeds": round(total_sell_proceeds, 2),
                    "realized_pnl": round(realized_pnl, 2),
                    "roi_pct": round(roi_pct, 1),
                    "avg_buy_price": round(avg_buy_price, 4),
                    "opened_at": opened_at,
                    "closed_at": closed_at,
                    "result": result,
                    "net_shares": round(net_shares, 4),
                    "side": "YES" if buys else ("NO" if sells else "-"),
                }
            )

        if net_shares > 0.0001:
            open_positions.append(
                {
                    "condition_id": cid,
                    "market_title": market["market_title"],
                    "net_shares": round(net_shares, 4),
                    "avg_buy_price": round(avg_buy_price, 4),
                    "cost_basis": round(net_shares * avg_buy_price, 2),
                    "opened_at": opened_at,
                    "side": "YES" if buys else "-",
                }
            )

    closed_positions.sort(key=lambda item: item["closed_at"] or datetime.min, reverse=True)
    open_positions.sort(key=lambda item: item["opened_at"] or datetime.min, reverse=True)

    now = datetime.utcnow()
    this_month, this_year = now.month, now.year
    last_month = 12 if this_month == 1 else this_month - 1
    last_year = this_year - 1 if this_month == 1 else this_year

    def _period_stats(positions: List[Dict[str, Any]], month: int, year: int) -> Dict[str, Any]:
        filtered = [
            p for p in positions
            if p["closed_at"] and p["closed_at"].month == month and p["closed_at"].year == year
        ]
        wins = sum(1 for p in filtered if p["result"] == "WIN")
        losses = sum(1 for p in filtered if p["result"] == "LOSS")
        total = wins + losses
        win_amounts = [p["realized_pnl"] for p in filtered if p["result"] == "WIN"]
        loss_amounts = [abs(p["realized_pnl"]) for p in filtered if p["result"] == "LOSS"]
        return {
            "pnl": round(sum(p["realized_pnl"] for p in filtered), 2),
            "wins": wins,
            "losses": losses,
            "count": len(filtered),
            "win_rate": round(wins / total * 100, 1) if total else 0,
            "success_rate": round(wins / total * 100, 1) if total else 0,
            "avg_win_usd": round(sum(win_amounts) / len(win_amounts), 2) if win_amounts else 0,
            "avg_loss_usd": round(sum(loss_amounts) / len(loss_amounts), 2) if loss_amounts else 0,
        }

    all_wins = sum(1 for p in closed_positions if p["result"] == "WIN")
    all_losses = sum(1 for p in closed_positions if p["result"] == "LOSS")
    all_total = all_wins + all_losses
    all_win_amounts = [p["realized_pnl"] for p in closed_positions if p["result"] == "WIN"]
    all_loss_amounts = [abs(p["realized_pnl"]) for p in closed_positions if p["result"] == "LOSS"]

    return {
        "closed": closed_positions,
        "open": open_positions,
        "summary": {
            "all_time": {
                "pnl": round(sum(p["realized_pnl"] for p in closed_positions), 2),
                "wins": all_wins,
                "losses": all_losses,
                "count": len(closed_positions),
                "win_rate": round(all_wins / all_total * 100, 1) if all_total else 0,
                "success_rate": round(all_wins / all_total * 100, 1) if all_total else 0,
                "avg_win_usd": round(sum(all_win_amounts) / len(all_win_amounts), 2) if all_win_amounts else 0,
                "avg_loss_usd": round(sum(all_loss_amounts) / len(all_loss_amounts), 2) if all_loss_amounts else 0,
            },
            "this_month": _period_stats(closed_positions, this_month, this_year),
            "last_month": _period_stats(closed_positions, last_month, last_year),
        },
        "total_cost": round(sum(p["cost_basis"] for p in open_positions), 2),
    }


def _apply_trade_filters(
    query,
    side: Optional[str] = None,
    market_search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    wallet_address: Optional[str] = None,
):
    if wallet_address:
        query = query.filter(Trade.wallet_address == wallet_address)
    if side and side in {"YES", "NO"}:
        query = query.filter(Trade.side == side)
    if market_search:
        term = f"%{market_search}%"
        query = query.filter(or_(Trade.market_title.ilike(term), Trade.condition_id.ilike(term)))
    if date_from:
        try:
            query = query.filter(Trade.traded_at >= datetime.fromisoformat(date_from))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(Trade.traded_at <= datetime.fromisoformat(date_to))
        except ValueError:
            pass
    return query


@router.get("/")
async def root():
    return RedirectResponse(url="/wallets", status_code=302)


@router.get("/dashboard")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "dashboard_v2.html",
        {
            "request": request,
            "stats": build_dashboard_stats(db),
            "wallets": _wallet_order_query(db).limit(5).all(),
        },
    )


@router.get("/wallets")
async def list_wallets(request: Request, db: Session = Depends(get_db)):
    wallets = _wallet_order_query(db).all()
    wallet_stats = {wallet.address: calculate_wallet_stats(db, wallet.address) for wallet in wallets}
    return templates.TemplateResponse(
        "wallets_v2.html",
        {"request": request, "wallets": wallets, "wallet_stats": wallet_stats},
    )


@router.post("/wallets")
async def add_wallet(
    address: str = Form(...),
    label: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    is_pinned: Optional[int] = Form(0),
    db: Session = Depends(get_db),
):
    address = address.strip().lower()
    if not address:
        raise HTTPException(status_code=400, detail="Address cannot be empty")
    if db.query(Wallet).filter(Wallet.address == address).first():
        raise HTTPException(status_code=400, detail="Wallet already exists")

    wallet = Wallet(
        address=address,
        label=label.strip() if label else None,
        tags=tags.strip() if tags else None,
        is_pinned=1 if int(is_pinned or 0) else 0,
    )
    db.add(wallet)
    db.commit()
    db.refresh(wallet)
    refresh_wallet(db, wallet)
    return RedirectResponse(url="/wallets", status_code=303)


@router.post("/wallets/{address}/update")
async def update_wallet(
    address: str,
    label: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    is_pinned: Optional[int] = Form(0),
    db: Session = Depends(get_db),
):
    wallet = resolve_wallet(db, address)
    wallet.label = label.strip() if label else None
    wallet.tags = tags.strip() if tags else None
    wallet.is_pinned = 1 if int(is_pinned or 0) else 0
    db.commit()
    return RedirectResponse(url="/wallets", status_code=303)


@router.post("/wallets/{address}/delete")
async def delete_wallet(address: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, address)
    db.query(Trade).filter(Trade.wallet_address == wallet.address).delete()
    db.delete(wallet)
    db.commit()
    return RedirectResponse(url="/wallets", status_code=303)


@router.get("/wallets/{address}/trades")
async def view_trades(
    request: Request,
    address: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    side: Optional[str] = Query(None),
    market_search: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    sort_by: str = Query("time_desc"),
    db: Session = Depends(get_db),
):
    wallet = resolve_wallet(db, address)
    query = _apply_trade_filters(
        db.query(Trade),
        side=side,
        market_search=market_search,
        date_from=date_from,
        date_to=date_to,
        wallet_address=wallet.address,
    )
    if sort_by == "time_asc":
        query = query.order_by(Trade.traded_at.asc())
    elif sort_by == "size_desc":
        query = query.order_by(Trade.size.desc())
    elif sort_by == "size_asc":
        query = query.order_by(Trade.size.asc())
    else:
        query = query.order_by(Trade.traded_at.desc())

    total_trades = query.count()
    trades = query.limit(page_size).offset((page - 1) * page_size).all()
    total_pages = (total_trades + page_size - 1) // page_size if total_trades else 1

    return templates.TemplateResponse(
        "trades_v2.html",
        {
            "request": request,
            "wallet": wallet,
            "trades": trades,
            "page": page,
            "page_size": page_size,
            "total_trades": total_trades,
            "total_pages": total_pages,
            "side": side,
            "market_search": market_search,
            "date_from": date_from,
            "date_to": date_to,
            "sort_by": sort_by,
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
    query = query.order_by(Trade.traded_at.desc())
    total_trades = query.count()
    trades = query.limit(page_size).offset((page - 1) * page_size).all()
    total_pages = (total_trades + page_size - 1) // page_size if total_trades else 1
    wallet_map = {wallet.address: wallet for wallet in db.query(Wallet).all()}
    return templates.TemplateResponse(
        "all_trades_v2.html",
        {
            "request": request,
            "trades": trades,
            "page": page,
            "page_size": page_size,
            "total_trades": total_trades,
            "total_pages": total_pages,
            "side": side,
            "market_search": market_search,
            "date_from": date_from,
            "date_to": date_to,
            "wallet_search": wallet_search,
            "wallet_map": wallet_map,
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
        .all()
    )
    wallet_map = {wallet.address: wallet for wallet in db.query(Wallet).all()}
    return templates.TemplateResponse(
        "trade_detail_v2.html",
        {
            "request": request,
            "trade": trade,
            "related_trades": related_trades,
            "wallet_map": wallet_map,
        },
    )


@router.get("/wallets/{address}/trades/export")
async def export_trades(
    address: str,
    side: Optional[str] = Query(None),
    market_search: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    sort_by: str = Query("time_desc"),
    db: Session = Depends(get_db),
):
    wallet = resolve_wallet(db, address)
    query = _apply_trade_filters(
        db.query(Trade),
        side=side,
        market_search=market_search,
        date_from=date_from,
        date_to=date_to,
        wallet_address=wallet.address,
    )
    if sort_by == "time_asc":
        query = query.order_by(Trade.traded_at.asc())
    elif sort_by == "size_desc":
        query = query.order_by(Trade.size.desc())
    elif sort_by == "size_asc":
        query = query.order_by(Trade.size.asc())
    else:
        query = query.order_by(Trade.traded_at.desc())

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


@router.get("/wallets/{address}/pnl")
async def view_pnl(
    request: Request,
    address: str,
    result: Optional[str] = Query(None),
    side: Optional[str] = Query(None),
    market_search: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    wallet = resolve_wallet(db, address)
    data = calculate_pnl_analytics(db, wallet.address)
    closed = data["closed"]
    if result in {"WIN", "LOSS", "BREAKEVEN"}:
        closed = [item for item in closed if item["result"] == result]
    if side in {"YES", "NO"}:
        closed = [item for item in closed if item["side"] == side]
    if market_search:
        closed = [item for item in closed if market_search.lower() in item["market_title"].lower()]
    if date_from:
        try:
            start = datetime.fromisoformat(date_from)
            closed = [item for item in closed if item["closed_at"] and item["closed_at"] >= start]
        except ValueError:
            pass
    if date_to:
        try:
            end = datetime.fromisoformat(date_to)
            closed = [item for item in closed if item["closed_at"] and item["closed_at"] <= end]
        except ValueError:
            pass
    return templates.TemplateResponse(
        "pnl_v2.html",
        {
            "request": request,
            "wallet": wallet,
            "summary": data["summary"],
            "closed": closed,
            "open": data["open"],
            "total_cost": data["total_cost"],
            "f_result": result or "",
            "f_side": side or "",
            "f_market": market_search or "",
            "f_date_from": date_from or "",
            "f_date_to": date_to or "",
        },
    )


@router.get("/charts")
async def charts_page(request: Request, db: Session = Depends(get_db)):
    daily_rows = (
        db.query(
            func.date(Trade.traded_at).label("day"),
            func.count(Trade.id).label("trade_count"),
            func.sum(Trade.size).label("volume"),
        )
        .group_by(func.date(Trade.traded_at))
        .order_by(func.date(Trade.traded_at).asc())
        .all()
    )
    side_rows = db.query(Trade.side, func.count(Trade.id).label("trade_count")).group_by(Trade.side).all()
    return templates.TemplateResponse(
        "charts_v2.html",
        {
            "request": request,
            "chart_data": json.dumps(
                {
                    "labels": [row.day for row in daily_rows],
                    "trades_per_day": [row.trade_count for row in daily_rows],
                    "volume_per_day": [round(row.volume or 0, 2) for row in daily_rows],
                    "side_labels": [row.side for row in side_rows],
                    "side_counts": [row.trade_count for row in side_rows],
                }
            ),
        },
    )


@router.get("/notifications")
async def view_notifications(request: Request, db: Session = Depends(get_db)):
    notifications = (
        db.query(Notification)
        .filter(Notification.created_at.isnot(None))
        .order_by(desc(Notification.created_at))
        .limit(100)
        .all()
    )
    wallet_map = {wallet.address: wallet for wallet in db.query(Wallet).all()}
    unread_count = db.query(Notification).filter(Notification.is_read == 0).count()
    return templates.TemplateResponse(
        "notifications_v2.html",
        {
            "request": request,
            "notifications": notifications,
            "wallet_map": wallet_map,
            "unread_count": unread_count,
        },
    )


@router.get("/settings/notifications")
async def notification_settings_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "notification_settings_v2.html",
        {"request": request, "settings": get_notification_settings(db)},
    )


@router.post("/settings/notifications")
async def update_notification_settings(
    sound_enabled: Optional[int] = Form(0),
    min_trade_value: Optional[float] = Form(0.0),
    dedupe_window_seconds: Optional[int] = Form(120),
    db: Session = Depends(get_db),
):
    settings = get_notification_settings(db)
    settings.sound_enabled = 1 if int(sound_enabled or 0) else 0
    settings.min_trade_value = float(min_trade_value or 0.0)
    settings.dedupe_window_seconds = int(dedupe_window_seconds or 0)
    db.commit()
    return RedirectResponse(url="/settings/notifications", status_code=303)


@router.post("/notifications/mark-read")
async def mark_notifications_read(db: Session = Depends(get_db)):
    db.query(Notification).update({"is_read": 1})
    db.commit()
    return JSONResponse({"status": "success"})


@router.get("/notifications/count")
async def get_notification_count(db: Session = Depends(get_db)):
    count = db.query(Notification).filter(Notification.is_read == 0).count()
    settings = get_notification_settings(db)
    return JSONResponse(
        {
            "count": count,
            "sound_enabled": bool(settings.sound_enabled),
            "min_trade_value": settings.min_trade_value or 0,
        }
    )


@router.get("/admin/sync-status")
async def sync_status_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "sync_status_v2.html",
        {
            "request": request,
            "events": db.query(SyncEvent).order_by(desc(SyncEvent.created_at)).limit(50).all(),
            "duplicates": find_duplicate_groups(db),
        },
    )


@router.post("/admin/sync-status/cleanup")
async def cleanup_sync_duplicates(db: Session = Depends(get_db)):
    return JSONResponse({"status": "success", "removed": cleanup_duplicate_trades(db)})


@router.post("/admin/refresh")
async def refresh_trades(
    address: Optional[str] = Query(None),
    limit_per_wallet: int = Query(200, ge=1, le=1000),
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
async def refresh_all_trades(address: Optional[str] = Query(None), db: Session = Depends(get_db)):
    if address:
        wallet = resolve_wallet(db, address)
        return JSONResponse(
            {"status": "success", **refresh_wallet(db, wallet, fetch_all=True), "message": "Full history fetch complete"}
        )

    results: Dict[str, Any] = {}
    for wallet in _wallet_order_query(db).all():
        results[wallet.address] = refresh_wallet(db, wallet, fetch_all=True)
    return JSONResponse(
        {
            "status": "success",
            "wallets_refreshed": len(results),
            "results": results,
            "message": "Full history fetch complete for all wallets",
        }
    )


@router.post("/api/wallet/{identifier}/refresh")
async def api_refresh_wallet(identifier: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, identifier)
    return JSONResponse({"status": "success", "wallet_id": wallet.id, **refresh_wallet(db, wallet)})


@router.get("/api/wallet/{identifier}")
async def api_wallet_summary(identifier: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, identifier)
    return JSONResponse(
        {
            "id": wallet.id,
            "address": wallet.address,
            "label": wallet.label,
            "tags": wallet.tags or "",
            "is_pinned": bool(wallet.is_pinned),
            "last_checked_at": wallet.last_checked_at.isoformat() if wallet.last_checked_at else None,
            "last_refresh_count": wallet.last_refresh_count or 0,
            "last_error_message": wallet.last_error_message,
            "stats": calculate_wallet_stats_snapshot(db, wallet.address),
        }
    )


@router.get("/live-updates")
async def live_updates(_: Request):
    import asyncio
    from app.live_events_v2 import event_subscribers

    async def event_generator():
        queue = asyncio.Queue()
        event_subscribers.append(queue)
        try:
            yield f"data: {json.dumps({'type': 'connected', 'message': 'Live updates connected'})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': datetime.utcnow().isoformat()})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if queue in event_subscribers:
                event_subscribers.remove(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/api/stats")
async def get_stats(db: Session = Depends(get_db)):
    stats = build_dashboard_stats(db)
    return JSONResponse(
        {
            "total_wallets": stats["total_wallets"],
            "total_trades": stats["total_trades"],
            "trades_today": stats["trades_today"],
            "recent_trades_24h": stats["recent_trades_24h"],
            "most_active_wallet": stats["most_active_wallet"],
            "most_active_wallet_count": stats["most_active_wallet_count"],
            "biggest_trade": serialize_trade(stats["biggest_trade"]) if stats["biggest_trade"] else None,
            "recent_alerts": [
                {
                    "id": n.id,
                    "wallet_address": n.wallet_address,
                    "message": n.message,
                    "created_at": n.created_at.isoformat() if n.created_at else None,
                }
                for n in stats["recent_alerts"]
            ],
        }
    )


@router.get("/api/wallet/{address}/pnl/summary")
async def api_pnl_summary(address: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, address)
    return JSONResponse(calculate_pnl_analytics(db, wallet.address)["summary"])


@router.get("/api/wallet/{address}/pnl/closed")
async def api_pnl_closed(
    address: str,
    result: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    wallet = resolve_wallet(db, address)
    closed = calculate_pnl_analytics(db, wallet.address)["closed"]
    if result:
        closed = [item for item in closed if item["result"] == result]
    if date_from:
        try:
            start = datetime.fromisoformat(date_from)
            closed = [item for item in closed if item["closed_at"] and item["closed_at"] >= start]
        except ValueError:
            pass
    if date_to:
        try:
            end = datetime.fromisoformat(date_to)
            closed = [item for item in closed if item["closed_at"] and item["closed_at"] <= end]
        except ValueError:
            pass
    for item in closed:
        item["opened_at"] = item["opened_at"].isoformat() if item["opened_at"] else None
        item["closed_at"] = item["closed_at"].isoformat() if item["closed_at"] else None
    return JSONResponse(closed)


@router.get("/api/wallet/{address}/pnl/open")
async def api_pnl_open(address: str, db: Session = Depends(get_db)):
    wallet = resolve_wallet(db, address)
    open_positions = calculate_pnl_analytics(db, wallet.address)["open"]
    for item in open_positions:
        item["opened_at"] = item["opened_at"].isoformat() if item["opened_at"] else None
    return JSONResponse(open_positions)
