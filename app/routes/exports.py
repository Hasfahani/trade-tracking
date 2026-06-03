"""CSV export routes."""
import csv
import io
import json
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.backup import build_backup
from app import view_helpers as vh
from app.db import get_db
from app.models import Trade, Wallet
from app.routes._shared import normalized_date_filters, resolve_wallet

_BOM = "\ufeff"
router = APIRouter()


@router.get("/admin/backup.json", summary="Export full JSON backup", tags=["exports"])
async def export_full_backup(db: Session = Depends(get_db)):
    payload = build_backup(db)
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    filename = f"polysignal_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    return StreamingResponse(
        iter([body]),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Backup-Checksum-SHA256": payload["checksum_sha256"],
        },
    )


@router.get("/wallets/export", summary="Export wallets as CSV", tags=["exports"])
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


@router.get("/all-trades/export", summary="Export all trades as CSV (filterable)", tags=["exports"])
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

    wallet_map = {w.address: w for w in db.query(Wallet).all()}
    trades = query.all()

    def _rows():
        yield _BOM
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Trade ID", "Date (UTC)", "Wallet", "Market Title", "Condition ID", "Side", "Price", "Size", "Value"])
        yield buf.getvalue()
        for trade in trades:
            buf = io.StringIO()
            writer = csv.writer(buf)
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
            yield buf.getvalue()

    filename = f"all_trades_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        _rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/wallets/{identifier}/trades/export", summary="Export single-wallet trades as CSV", tags=["exports"])
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
    date_from, date_to = normalized_date_filters(date_preset, date_from, date_to)
    query = vh.apply_trade_filters(
        db.query(Trade),
        wallet_address=wallet.address,
        side=side,
        market_search=market_search,
        date_from=date_from,
        date_to=date_to,
    )
    query = vh.sorted_trade_query(query, sort_by)

    trades = query.all()

    def _rows():
        yield _BOM
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Trade ID", "Date (UTC)", "Market Title", "Condition ID", "Side", "Price", "Size", "Value"])
        yield buf.getvalue()
        for trade in trades:
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow([
                trade.trade_id,
                trade.traded_at.strftime("%Y-%m-%d %H:%M:%S"),
                trade.market_title or "N/A",
                trade.condition_id,
                trade.side,
                f"{trade.price:.4f}",
                f"{trade.size:.2f}",
                f"{(trade.price * trade.size):.2f}",
            ])
            yield buf.getvalue()

    filename = f"trades_{wallet.address[:8]}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        _rows(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
