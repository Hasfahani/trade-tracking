from fastapi import APIRouter, Depends, Form, Query, HTTPException, Request
from fastapi.responses import RedirectResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
import csv
import io
from app.db import get_db
from app.models import Wallet, Trade
from app.ingest import ingest_trades, refresh_all_wallets
from app.settings import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def calculate_wallet_stats(db: Session, wallet_address: str) -> Dict[str, Any]:
    """Calculate stats for a wallet."""
    trades = db.query(Trade).filter(Trade.wallet_address == wallet_address).all()
    
    if not trades:
        return {
            "total_trades": 0,
            "total_volume": 0,
            "unique_markets": 0,
            "yes_trades": 0,
            "no_trades": 0,
            "avg_trade_size": 0,
            "last_trade_date": None
        }
    
    total_volume = sum(t.size for t in trades)
    yes_trades = sum(1 for t in trades if t.side == "YES")
    no_trades = sum(1 for t in trades if t.side == "NO")
    unique_markets = len(set(t.condition_id for t in trades))
    last_trade = max(trades, key=lambda t: t.traded_at)
    
    return {
        "total_trades": len(trades),
        "total_volume": round(total_volume, 2),
        "unique_markets": unique_markets,
        "yes_trades": yes_trades,
        "no_trades": no_trades,
        "avg_trade_size": round(total_volume / len(trades), 2),
        "last_trade_date": last_trade.traded_at
    }


def calculate_pnl_analytics(db: Session, wallet_address: str) -> Dict[str, Any]:
    """
    Full P&L analytics for a wallet.

    Data mapping note:
      In the ingestion layer, the Polymarket API 'side' field (BUY/SELL) was mapped
      to our Trade.side column: BUY → "YES", SELL → "NO".
      Trades with side="YES" are treated as buys (money out).
      Trades with side="NO" are treated as sells / partial closes (money in).
      Positions are grouped solely by condition_id.
    """
    trades: List[Trade] = (
        db.query(Trade)
        .filter(Trade.wallet_address == wallet_address)
        .order_by(Trade.traded_at)
        .all()
    )

    # ── Group trades by market ────────────────────────────
    markets: Dict[str, Dict] = {}
    for t in trades:
        cid = t.condition_id
        if cid not in markets:
            markets[cid] = {
                "condition_id": cid,
                "market_title": t.market_title or "Unknown Market",
                "buys": [],
                "sells": [],
            }
        m = markets[cid]
        # Refresh title in case it was updated
        if t.market_title:
            m["market_title"] = t.market_title
        if t.side == "YES":
            m["buys"].append(t)
        else:
            m["sells"].append(t)

    # ── Build position records ────────────────────────────
    closed_positions: List[Dict] = []
    open_positions: List[Dict] = []

    for cid, m in markets.items():
        buys  = m["buys"]
        sells = m["sells"]

        total_buy_shares   = sum(t.size for t in buys)
        total_buy_cost_usd = sum(t.price * t.size for t in buys)
        total_sell_shares  = sum(t.size for t in sells)
        total_sell_proceed = sum(t.price * t.size for t in sells)

        avg_buy_price = (total_buy_cost_usd / total_buy_shares) if total_buy_shares > 0 else 0.0

        # Closed = the min of buy/sell shares (fully or partially closed)
        closed_shares = min(total_buy_shares, total_sell_shares)
        net_shares    = total_buy_shares - total_sell_shares

        opened_at = buys[0].traded_at  if buys  else None
        closed_at = sells[-1].traded_at if sells else None

        # ── Realized P&L for the closed portion ──────────
        if closed_shares > 0.0001:
            realized_pnl = total_sell_proceed - (closed_shares * avg_buy_price)
            amount_bet   = closed_shares * avg_buy_price
            roi_pct      = (realized_pnl / amount_bet * 100) if amount_bet > 0 else 0.0

            if realized_pnl > 0.005:
                result = "WIN"
            elif realized_pnl < -0.005:
                result = "LOSS"
            else:
                result = "BREAKEVEN"

            closed_positions.append({
                "condition_id":   cid,
                "market_title":   m["market_title"],
                "buy_shares":     round(total_buy_shares, 4),
                "sell_shares":    round(total_sell_shares, 4),
                "amount_bet":     round(amount_bet, 2),
                "proceeds":       round(total_sell_proceed, 2),
                "realized_pnl":   round(realized_pnl, 2),
                "roi_pct":        round(roi_pct, 1),
                "avg_buy_price":  round(avg_buy_price, 4),
                "opened_at":      opened_at,
                "closed_at":      closed_at,
                "result":         result,
                "net_shares":     round(net_shares, 4),
                # Used for side display
                "side":           "YES" if buys else ("NO" if sells else "—"),
            })

        # ── Open position (remaining shares not yet sold) ──
        if net_shares > 0.0001:
            open_cost = net_shares * avg_buy_price
            open_positions.append({
                "condition_id":  cid,
                "market_title":  m["market_title"],
                "net_shares":    round(net_shares, 4),
                "avg_buy_price": round(avg_buy_price, 4),
                "cost_basis":    round(open_cost, 2),
                "opened_at":     opened_at,
                "side":          "YES" if buys else "—",
            })

    # Sort
    closed_positions.sort(key=lambda x: x["closed_at"] or datetime.min, reverse=True)
    open_positions.sort(key=lambda x: x["opened_at"] or datetime.min, reverse=True)

    # ── Summary helpers ───────────────────────────────────
    now = datetime.utcnow()
    this_month, this_year = now.month, now.year
    last_month = 12 if this_month == 1 else this_month - 1
    last_year  = this_year - 1 if this_month == 1 else this_year

    def _period_stats(positions: List[Dict], month: int, year: int) -> Dict:
        pp = [p for p in positions
              if p["closed_at"]
              and p["closed_at"].month == month
              and p["closed_at"].year == year]
        wins   = sum(1 for p in pp if p["result"] == "WIN")
        losses = sum(1 for p in pp if p["result"] == "LOSS")
        total  = wins + losses
        win_amounts  = [p["realized_pnl"] for p in pp if p["result"] == "WIN"]
        loss_amounts = [abs(p["realized_pnl"]) for p in pp if p["result"] == "LOSS"]
        return {
            "pnl":              round(sum(p["realized_pnl"] for p in pp), 2),
            "wins":             wins,
            "losses":           losses,
            "count":            len(pp),
            "win_rate":         round(wins / total * 100, 1) if total > 0 else 0,
            "success_rate":     round(wins / total * 100, 1) if total > 0 else 0,
            "avg_win_usd":      round(sum(win_amounts)  / len(win_amounts),  2) if win_amounts  else 0,
            "avg_loss_usd":     round(sum(loss_amounts) / len(loss_amounts), 2) if loss_amounts else 0,
        }

    all_wins   = sum(1 for p in closed_positions if p["result"] == "WIN")
    all_losses = sum(1 for p in closed_positions if p["result"] == "LOSS")
    all_total  = all_wins + all_losses
    all_win_amounts  = [p["realized_pnl"] for p in closed_positions if p["result"] == "WIN"]
    all_loss_amounts = [abs(p["realized_pnl"]) for p in closed_positions if p["result"] == "LOSS"]

    summary = {
        "all_time": {
            "pnl":          round(sum(p["realized_pnl"] for p in closed_positions), 2),
            "wins":         all_wins,
            "losses":       all_losses,
            "count":        len(closed_positions),
            "win_rate":     round(all_wins / all_total * 100, 1) if all_total > 0 else 0,
            "success_rate": round(all_wins / all_total * 100, 1) if all_total > 0 else 0,
            "avg_win_usd":  round(sum(all_win_amounts)  / len(all_win_amounts),  2) if all_win_amounts  else 0,
            "avg_loss_usd": round(sum(all_loss_amounts) / len(all_loss_amounts), 2) if all_loss_amounts else 0,
        },
        "this_month": _period_stats(closed_positions, this_month, this_year),
        "last_month": _period_stats(closed_positions, last_month, last_year),
    }

    return {
        "closed":       closed_positions,
        "open":         open_positions,
        "summary":      summary,
        "total_cost":   round(sum(p["cost_basis"] for p in open_positions), 2),
    }



@router.get("/")
async def root():
    """Redirect to wallets page."""
    return RedirectResponse(url="/wallets", status_code=302)


@router.get("/wallets")
async def list_wallets(request: Request, db: Session = Depends(get_db)):
    """List all wallets with stats."""
    wallets = db.query(Wallet).order_by(desc(Wallet.created_at)).all()
    
    # Calculate stats for each wallet
    wallet_stats = {}
    for wallet in wallets:
        wallet_stats[wallet.address] = calculate_wallet_stats(db, wallet.address)
    
    return templates.TemplateResponse(
        "wallets.html",
        {"request": request, "wallets": wallets, "wallet_stats": wallet_stats}
    )


@router.post("/wallets")
async def add_wallet(
    address: str = Form(...),
    label: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """Add a new wallet and fetch its trades."""
    address = address.strip().lower()
    
    if not address:
        raise HTTPException(status_code=400, detail="Address cannot be empty")
    
    # Check if wallet already exists
    existing = db.query(Wallet).filter(Wallet.address == address).first()
    if existing:
        raise HTTPException(status_code=400, detail="Wallet already exists")
    
    wallet = Wallet(address=address, label=label.strip() if label else None)
    db.add(wallet)
    db.commit()
    
    # Automatically fetch trades for the new wallet
    try:
        ingest_trades(db, address)
    except Exception as e:
        print(f"Error fetching trades for new wallet {address}: {e}")
    
    return RedirectResponse(url="/wallets", status_code=303)


@router.post("/wallets/{address}/delete")
async def delete_wallet(address: str, db: Session = Depends(get_db)):
    """Delete a wallet and its trades."""
    address = address.strip().lower()
    
    wallet = db.query(Wallet).filter(Wallet.address == address).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    
    # Delete trades first (cascade)
    db.query(Trade).filter(Trade.wallet_address == address).delete()
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
    db: Session = Depends(get_db)
):
    """View paginated and filtered trades for a wallet."""
    address = address.strip().lower()
    
    # Verify wallet exists
    wallet = db.query(Wallet).filter(Wallet.address == address).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    
    # Build base query
    query = db.query(Trade).filter(Trade.wallet_address == address)
    
    # Apply filters
    if side and side in ["YES", "NO"]:
        query = query.filter(Trade.side == side)
    
    if market_search:
        search_term = f"%{market_search}%"
        query = query.filter(
            (Trade.market_title.ilike(search_term)) | 
            (Trade.condition_id.ilike(search_term))
        )
    
    if date_from:
        try:
            from datetime import datetime
            date_from_dt = datetime.fromisoformat(date_from)
            query = query.filter(Trade.traded_at >= date_from_dt)
        except ValueError:
            pass
    
    if date_to:
        try:
            from datetime import datetime
            date_to_dt = datetime.fromisoformat(date_to)
            query = query.filter(Trade.traded_at <= date_to_dt)
        except ValueError:
            pass
    
    # Apply sorting
    if sort_by == "time_asc":
        query = query.order_by(Trade.traded_at.asc())
    elif sort_by == "size_desc":
        query = query.order_by(Trade.size.desc())
    elif sort_by == "size_asc":
        query = query.order_by(Trade.size.asc())
    else:  # time_desc (default)
        query = query.order_by(Trade.traded_at.desc())
    
    # Get total count with filters
    total_trades = query.count()
    
    # Get paginated trades
    offset = (page - 1) * page_size
    trades = query.limit(page_size).offset(offset).all()
    
    # Calculate pagination info
    total_pages = (total_trades + page_size - 1) // page_size if total_trades > 0 else 1
    
    return templates.TemplateResponse(
        "trades.html",
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
        }
    )


@router.get("/wallets/{address}/trades/export")
async def export_trades(
    address: str,
    side: Optional[str] = Query(None),
    market_search: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    sort_by: str = Query("time_desc"),
    db: Session = Depends(get_db)
):
    """Export filtered trades to CSV."""
    address = address.strip().lower()
    
    # Verify wallet exists
    wallet = db.query(Wallet).filter(Wallet.address == address).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    
    # Build query with same filters as view_trades
    query = db.query(Trade).filter(Trade.wallet_address == address)
    
    if side and side in ["YES", "NO"]:
        query = query.filter(Trade.side == side)
    
    if market_search:
        search_term = f"%{market_search}%"
        query = query.filter(
            (Trade.market_title.ilike(search_term)) | 
            (Trade.condition_id.ilike(search_term))
        )
    
    if date_from:
        try:
            date_from_dt = datetime.fromisoformat(date_from)
            query = query.filter(Trade.traded_at >= date_from_dt)
        except ValueError:
            pass
    
    if date_to:
        try:
            date_to_dt = datetime.fromisoformat(date_to)
            query = query.filter(Trade.traded_at <= date_to_dt)
        except ValueError:
            pass
    
    # Apply sorting
    if sort_by == "time_asc":
        query = query.order_by(Trade.traded_at.asc())
    elif sort_by == "size_desc":
        query = query.order_by(Trade.size.desc())
    elif sort_by == "size_asc":
        query = query.order_by(Trade.size.asc())
    else:
        query = query.order_by(Trade.traded_at.desc())
    
    # Get all trades (no pagination for export)
    trades = query.all()
    
    # Create CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow([
        "Trade ID",
        "Date (UTC)",
        "Market Title",
        "Condition ID",
        "Side",
        "Price",
        "Size",
        "Value (Price × Size)"
    ])
    
    # Write data rows
    for trade in trades:
        writer.writerow([
            trade.trade_id,
            trade.traded_at.strftime('%Y-%m-%d %H:%M:%S'),
            trade.market_title or "N/A",
            trade.condition_id,
            trade.side,
            f"{trade.price:.4f}",
            f"{trade.size:.2f}",
            f"{(trade.price * trade.size):.2f}"
        ])
    
    # Prepare response
    output.seek(0)
    timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    filename = f"trades_{address[:8]}_{timestamp}.csv"
    
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.get("/wallets/{address}/pnl")
async def view_pnl(
    request: Request,
    address: str,
    # Filters for closed positions table
    result: Optional[str] = Query(None),          # WIN / LOSS / BREAKEVEN
    side: Optional[str] = Query(None),             # YES / NO
    market_search: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """View detailed profit/loss analytics for a wallet."""
    address = address.strip().lower()

    wallet = db.query(Wallet).filter(Wallet.address == address).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    data = calculate_pnl_analytics(db, address)
    closed = data["closed"]

    # Apply filters to closed positions
    if result and result in ("WIN", "LOSS", "BREAKEVEN"):
        closed = [p for p in closed if p["result"] == result]
    if side and side in ("YES", "NO"):
        closed = [p for p in closed if p.get("side") == side]
    if market_search:
        q = market_search.lower()
        closed = [p for p in closed if q in p["market_title"].lower()]
    if date_from:
        try:
            df = datetime.fromisoformat(date_from)
            closed = [p for p in closed if p["closed_at"] and p["closed_at"] >= df]
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.fromisoformat(date_to)
            closed = [p for p in closed if p["closed_at"] and p["closed_at"] <= dt]
        except ValueError:
            pass

    return templates.TemplateResponse(
        "pnl.html",
        {
            "request":       request,
            "wallet":        wallet,
            "summary":       data["summary"],
            "closed":        closed,
            "open":          data["open"],
            "total_cost":    data["total_cost"],
            # Filter state (echo back)
            "f_result":      result or "",
            "f_side":        side or "",
            "f_market":      market_search or "",
            "f_date_from":   date_from or "",
            "f_date_to":     date_to or "",
        }
    )


# ── JSON API endpoints ────────────────────────────────────

@router.get("/api/wallet/{address}/pnl/summary")
async def api_pnl_summary(address: str, db: Session = Depends(get_db)):
    """Return P&L summary stats as JSON."""
    address = address.strip().lower()
    wallet = db.query(Wallet).filter(Wallet.address == address).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    data = calculate_pnl_analytics(db, address)
    return JSONResponse(data["summary"])


@router.get("/api/wallet/{address}/pnl/closed")
async def api_pnl_closed(
    address: str,
    result:         Optional[str] = Query(None),
    date_from:      Optional[str] = Query(None),
    date_to:        Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Return filtered closed positions as JSON."""
    address = address.strip().lower()
    wallet = db.query(Wallet).filter(Wallet.address == address).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    data = calculate_pnl_analytics(db, address)
    closed = data["closed"]
    if result:
        closed = [p for p in closed if p["result"] == result]
    if date_from:
        try:
            df = datetime.fromisoformat(date_from)
            closed = [p for p in closed if p["closed_at"] and p["closed_at"] >= df]
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.fromisoformat(date_to)
            closed = [p for p in closed if p["closed_at"] and p["closed_at"] <= dt]
        except ValueError:
            pass
    # Serialize datetimes
    for p in closed:
        p["opened_at"] = p["opened_at"].isoformat() if p["opened_at"] else None
        p["closed_at"] = p["closed_at"].isoformat() if p["closed_at"] else None
    return JSONResponse(closed)


@router.get("/api/wallet/{address}/pnl/open")
async def api_pnl_open(address: str, db: Session = Depends(get_db)):
    """Return open positions as JSON."""
    address = address.strip().lower()
    wallet = db.query(Wallet).filter(Wallet.address == address).first()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    data = calculate_pnl_analytics(db, address)
    for p in data["open"]:
        p["opened_at"] = p["opened_at"].isoformat() if p["opened_at"] else None
    return JSONResponse(data["open"])



@router.get("/notifications")
async def view_notifications(
    request: Request,
    db: Session = Depends(get_db)
):
    """View all notifications."""
    from app.models import Notification, Wallet
    from sqlalchemy import desc
    
    # Get all notifications with wallet info (filter out any with NULL created_at)
    notifications = db.query(Notification).filter(Notification.created_at.isnot(None)).order_by(desc(Notification.created_at)).limit(100).all()
    
    # Get wallet labels
    wallet_map = {w.address: w for w in db.query(Wallet).all()}
    
    # Count unread
    unread_count = db.query(Notification).filter(Notification.is_read == 0).count()
    
    return templates.TemplateResponse(
        "notifications.html",
        {
            "request": request,
            "notifications": notifications,
            "wallet_map": wallet_map,
            "unread_count": unread_count,
        }
    )


@router.post("/notifications/mark-read")
async def mark_notifications_read(db: Session = Depends(get_db)):
    """Mark all notifications as read."""
    from app.models import Notification
    
    db.query(Notification).update({"is_read": 1})
    db.commit()
    
    return JSONResponse({"status": "success"})


@router.get("/notifications/count")
async def get_notification_count(db: Session = Depends(get_db)):
    """Get unread notification count."""
    from app.models import Notification
    
    count = db.query(Notification).filter(Notification.is_read == 0).count()
    return JSONResponse({"count": count})


@router.post("/admin/refresh")
async def refresh_trades(
    address: Optional[str] = Query(None),
    limit_per_wallet: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db)
):
    """
    Refresh trades for all wallets or a specific wallet.
    Returns JSON stats.
    """
    if address:
        # Refresh single wallet
        address = address.strip().lower()
        wallet = db.query(Wallet).filter(Wallet.address == address).first()
        if not wallet:
            raise HTTPException(status_code=404, detail="Wallet not found")
        
        count = ingest_trades(db, address)
        return JSONResponse({
            "status": "success",
            "wallet": address,
            "trades_count": count
        })
    else:
        # Refresh all wallets
        results = refresh_all_wallets(db)
        
        return JSONResponse({
            "status": "success",
            "wallets_refreshed": len(results),
            "results": results
        })


@router.post("/admin/refresh-all")
async def refresh_all_trades(
    address: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Fetch ALL trades with pagination (no limit).
    This can take longer but ensures complete trade history.
    """
    from app.ingest import fetch_trades_for_wallet, normalize_trade
    from app.models import Trade
    from sqlalchemy.dialects.sqlite import insert
    
    if address:
        # Refresh single wallet with pagination
        address = address.strip().lower()
        wallet = db.query(Wallet).filter(Wallet.address == address).first()
        if not wallet:
            raise HTTPException(status_code=404, detail="Wallet not found")
        
        # Fetch ALL trades with pagination
        raw_trades = fetch_trades_for_wallet(address, limit=1000, fetch_all=True)
        
        # Normalize and insert
        inserted = 0
        for raw in raw_trades:
            trade = normalize_trade(raw, address)
            if trade:
                stmt = insert(Trade).values(
                    trade_id=trade["id"],
                    wallet_address=trade["wallet_address"],
                    condition_id=trade["condition_id"],
                    market_title=trade["market_title"],
                    side=trade["side"],
                    price=trade["price"],
                    size=trade["size"],
                    traded_at=trade["traded_at"],
                ).prefix_with("OR IGNORE")
                result = db.execute(stmt)
                if result.rowcount > 0:
                    inserted += 1
        
        db.commit()
        
        return JSONResponse({
            "status": "success",
            "wallet": address,
            "fetched": len(raw_trades),
            "inserted": inserted,
            "message": "Full history fetch complete"
        })
    else:
        # Refresh all wallets with pagination
        wallets = db.query(Wallet).all()
        results = {}
        
        for wallet in wallets:
            raw_trades = fetch_trades_for_wallet(wallet.address, limit=1000, fetch_all=True)
            
            inserted = 0
            for raw in raw_trades:
                trade = normalize_trade(raw, wallet.address)
                if trade:
                    stmt = insert(Trade).values(
                        trade_id=trade["id"],
                        wallet_address=trade["wallet_address"],
                        condition_id=trade["condition_id"],
                        market_title=trade["market_title"],
                        side=trade["side"],
                        price=trade["price"],
                        size=trade["size"],
                        traded_at=trade["traded_at"],
                    ).prefix_with("OR IGNORE")
                    result = db.execute(stmt)
                    if result.rowcount > 0:
                        inserted += 1
            
            db.commit()
            results[wallet.address] = {"fetched": len(raw_trades), "inserted": inserted}
        
        return JSONResponse({
            "status": "success",
            "wallets_refreshed": len(results),
            "results": results,
            "message": "Full history fetch complete for all wallets"
        })


@router.get("/live-updates")
async def live_updates(request: Request):
    """Server-Sent Events endpoint for live trade updates."""
    import asyncio
    import json
    from app.live_events import event_subscribers
    
    async def event_generator():
        # Create a queue for this client
        queue = asyncio.Queue()
        event_subscribers.append(queue)
        
        try:
            # Send initial connection message
            yield f"data: {json.dumps({'type': 'connected', 'message': 'Live updates connected'})}\n\n"
            
            # Send heartbeat and listen for events
            while True:
                try:
                    # Wait for event with timeout for heartbeat
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': datetime.utcnow().isoformat()})}\n\n"
                    
        except asyncio.CancelledError:
            pass
        finally:
            # Clean up when client disconnects
            if queue in event_subscribers:
                event_subscribers.remove(queue)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@router.get("/api/stats")
async def get_stats(db: Session = Depends(get_db)):
    """Get overall statistics for dashboard."""
    from app.models import Wallet, Trade
    
    total_wallets = db.query(Wallet).count()
    total_trades = db.query(Trade).count()
    
    # Recent trades count (last 24 hours)
    from datetime import datetime, timedelta
    yesterday = datetime.utcnow() - timedelta(days=1)
    recent_trades = db.query(Trade).filter(Trade.traded_at >= yesterday).count()
    
    return JSONResponse({
        "total_wallets": total_wallets,
        "total_trades": total_trades,
        "recent_trades_24h": recent_trades
    })
