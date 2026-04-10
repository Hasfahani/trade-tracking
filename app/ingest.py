import hashlib
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from app.live_events_v2 import broadcast_event
from app.models import Notification, NotificationSetting, SyncEvent, Trade, Wallet

# Public Polymarket Data API - NO AUTH REQUIRED
DATA_API_BASE = "https://data-api.polymarket.com"


def fetch_trades_for_wallet(address: str, limit: int = 1000, fetch_all: bool = False) -> List[Dict[str, Any]]:
    """Fetch wallet trades from the Polymarket public data API."""
    address = address.lower().strip()
    all_trades: List[Dict[str, Any]] = []

    try:
        if fetch_all:
            offset = 0
            while True:
                url = f"{DATA_API_BASE}/trades"
                params = {"user": address, "limit": limit, "offset": offset}

                with httpx.Client(timeout=30) as client:
                    response = client.get(url, params=params)
                    response.raise_for_status()
                    trades = response.json()

                if not trades:
                    break

                all_trades.extend(trades)
                if len(trades) < limit:
                    break
                offset += limit
        else:
            url = f"{DATA_API_BASE}/trades"
            params = {"user": address, "limit": limit}

            with httpx.Client(timeout=30) as client:
                response = client.get(url, params=params)
                response.raise_for_status()
                all_trades = response.json()

        return all_trades
    except Exception as e:
        print(f"Error fetching trades for {address}: {e}")
        raise


def normalize_trade(raw: Dict[str, Any], wallet_address: str) -> Optional[Dict[str, Any]]:
    """Normalize a Polymarket trade payload to the local schema."""
    try:
        tx_hash = raw.get("transactionHash", "")
        asset = raw.get("asset", "")
        if not tx_hash:
            return None

        trade_id = hashlib.sha256(f"{tx_hash}:{asset}".encode()).hexdigest()[:16]
        condition_id = raw.get("conditionId", "")
        market_title = raw.get("title", "Unknown Market")
        side_raw = raw.get("side", "").upper()
        outcome = raw.get("outcome", "")

        if side_raw == "BUY":
            side = "YES"
        elif side_raw == "SELL":
            side = "NO"
        else:
            side = outcome.upper() if outcome else "YES"

        price = float(raw.get("price", 0))
        size = float(raw.get("size", 0))
        if price <= 0 or size <= 0:
            return None

        ts = raw.get("timestamp")
        if isinstance(ts, (int, float)):
            traded_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            traded_at = datetime.now(timezone.utc)

        return {
            "id": trade_id,
            "wallet_address": wallet_address.lower(),
            "condition_id": condition_id,
            "market_title": market_title,
            "side": side,
            "price": price,
            "size": size,
            "traded_at": traded_at,
        }
    except Exception as e:
        print(f"Error normalizing trade: {e}")
        return None


def get_notification_settings(db: Session) -> NotificationSetting:
    """Return the singleton notification settings row."""
    settings = db.query(NotificationSetting).order_by(NotificationSetting.id.asc()).first()
    if settings:
        return settings

    settings = NotificationSetting(
        sound_enabled=1,
        min_trade_value=0.0,
        dedupe_window_seconds=120,
    )
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def calculate_wallet_stats_snapshot(db: Session, wallet_address: str) -> Dict[str, Any]:
    """Compute compact wallet stats for live updates and inline refreshes."""
    trades = db.query(Trade).filter(Trade.wallet_address == wallet_address).all()
    if not trades:
        return {
            "total_trades": 0,
            "total_volume": 0,
            "unique_markets": 0,
            "yes_trades": 0,
            "no_trades": 0,
            "avg_trade_size": 0,
            "last_trade_date": None,
        }

    total_volume = sum(t.size for t in trades)
    yes_trades = sum(1 for t in trades if t.side == "YES")
    no_trades = sum(1 for t in trades if t.side == "NO")
    unique_markets = len({t.condition_id for t in trades})
    last_trade = max(trades, key=lambda t: t.traded_at)
    return {
        "total_trades": len(trades),
        "total_volume": round(total_volume, 2),
        "unique_markets": unique_markets,
        "yes_trades": yes_trades,
        "no_trades": no_trades,
        "avg_trade_size": round(total_volume / len(trades), 2),
        "last_trade_date": last_trade.traded_at.isoformat() if last_trade.traded_at else None,
    }


def find_duplicate_groups(db: Session, wallet_address: Optional[str] = None) -> List[Dict[str, Any]]:
    """Detect semantically duplicate trades."""
    query = (
        db.query(
            Trade.wallet_address,
            Trade.condition_id,
            Trade.side,
            Trade.price,
            Trade.size,
            Trade.traded_at,
            func.count(Trade.id).label("duplicate_count"),
        )
        .group_by(
            Trade.wallet_address,
            Trade.condition_id,
            Trade.side,
            Trade.price,
            Trade.size,
            Trade.traded_at,
        )
        .having(func.count(Trade.id) > 1)
        .order_by(func.count(Trade.id).desc(), Trade.traded_at.desc())
    )
    if wallet_address:
        query = query.filter(Trade.wallet_address == wallet_address)

    return [
        {
            "wallet_address": row.wallet_address,
            "condition_id": row.condition_id,
            "side": row.side,
            "price": row.price,
            "size": row.size,
            "traded_at": row.traded_at,
            "duplicate_count": row.duplicate_count,
        }
        for row in query.all()
    ]


def cleanup_duplicate_trades(db: Session) -> int:
    """Delete duplicate semantic trade rows while keeping the oldest row in each group."""
    removed = 0
    for group in find_duplicate_groups(db):
        duplicates = (
            db.query(Trade)
            .filter(Trade.wallet_address == group["wallet_address"])
            .filter(Trade.condition_id == group["condition_id"])
            .filter(Trade.side == group["side"])
            .filter(Trade.price == group["price"])
            .filter(Trade.size == group["size"])
            .filter(Trade.traded_at == group["traded_at"])
            .order_by(Trade.id.asc())
            .all()
        )
        for trade in duplicates[1:]:
            db.delete(trade)
            removed += 1

    if removed:
        db.commit()
    return removed


def _create_sync_event(
    db: Session,
    wallet_address: str,
    status: str,
    fetched_count: int = 0,
    inserted_count: int = 0,
    error_message: Optional[str] = None,
) -> None:
    duplicate_count = sum(group["duplicate_count"] - 1 for group in find_duplicate_groups(db, wallet_address))
    db.add(
        SyncEvent(
            wallet_address=wallet_address,
            status=status,
            fetched_count=fetched_count,
            inserted_count=inserted_count,
            error_message=error_message,
            duplicate_count=duplicate_count,
        )
    )


async def _emit_live_refresh(wallet: Wallet, stats: Dict[str, Any], inserted_count: int) -> None:
    await broadcast_event(
        "wallet_refreshed",
        {
            "wallet_address": wallet.address,
            "last_checked_at": wallet.last_checked_at.isoformat() if wallet.last_checked_at else None,
            "last_refresh_count": inserted_count,
            "stats": stats,
            "label": wallet.label,
            "tags": wallet.tags or "",
        },
    )


async def _emit_live_trade(trade: Trade, wallet: Wallet, stats: Dict[str, Any]) -> None:
    await broadcast_event(
        "new_trade",
        {
            "wallet_address": trade.wallet_address,
            "trade_id": trade.trade_id,
            "market_title": trade.market_title,
            "side": trade.side,
            "price": float(trade.price),
            "size": float(trade.size),
            "traded_at": trade.traded_at.isoformat(),
            "wallet_label": wallet.label,
            "stats": stats,
            "last_checked_at": wallet.last_checked_at.isoformat() if wallet.last_checked_at else None,
        },
    )


def _dispatch_async(coro) -> None:
    """Run an async broadcast whether or not an event loop is already active."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return

    loop.create_task(coro)


def _should_create_notification(
    db: Session,
    trade: Trade,
    settings: NotificationSetting,
) -> bool:
    min_trade_value = settings.min_trade_value or 0.0
    if (trade.price * trade.size) < min_trade_value:
        return False

    dedupe_seconds = settings.dedupe_window_seconds or 0
    if dedupe_seconds <= 0:
        return True

    dedupe_after = datetime.now(timezone.utc) - timedelta(seconds=dedupe_seconds)
    existing = (
        db.query(Notification)
        .filter(Notification.trade_id == trade.trade_id)
        .filter(Notification.created_at >= dedupe_after)
        .first()
    )
    return existing is None


def refresh_wallet(
    db: Session,
    wallet: Wallet,
    *,
    fetch_all: bool = False,
    limit: int = 1000,
) -> Dict[str, Any]:
    """Refresh a wallet, log sync status, create notifications, and return a structured result."""
    now = datetime.now(timezone.utc)
    wallet.last_checked_at = now
    wallet.last_error_at = None
    wallet.last_error_message = None

    try:
        raw_trades = fetch_trades_for_wallet(wallet.address, limit=limit, fetch_all=fetch_all)
        normalized = [trade for trade in (normalize_trade(raw, wallet.address) for raw in raw_trades) if trade]

        inserted = 0
        new_trade_ids: List[str] = []
        for trade in normalized:
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
                new_trade_ids.append(trade["id"])

        wallet.last_refresh_count = inserted
        _create_sync_event(
            db,
            wallet.address,
            status="success",
            fetched_count=len(raw_trades),
            inserted_count=inserted,
        )
        db.commit()
        db.refresh(wallet)

        new_trades: List[Trade] = []
        if new_trade_ids:
            new_trades = (
                db.query(Trade)
                .filter(Trade.trade_id.in_(new_trade_ids))
                .order_by(Trade.traded_at.desc())
                .all()
            )

        settings = get_notification_settings(db)
        stats = calculate_wallet_stats_snapshot(db, wallet.address)

        for trade in new_trades:
            if _should_create_notification(db, trade, settings):
                db.add(
                    Notification(
                        wallet_address=trade.wallet_address,
                        trade_id=trade.trade_id,
                        message=f"New {trade.side} trade: {trade.size:.2f} @ ${trade.price:.4f}",
                        market_title=trade.market_title,
                        side=trade.side,
                        price=trade.price,
                        size=trade.size,
                        is_read=0,
                        created_at=datetime.now(timezone.utc),
                    )
                )
        db.commit()

        for trade in new_trades:
            _dispatch_async(_emit_live_trade(trade, wallet, stats))
        _dispatch_async(_emit_live_refresh(wallet, stats, inserted))

        return {
            "wallet": wallet.address,
            "fetched": len(raw_trades),
            "inserted": inserted,
            "stats": stats,
            "last_checked_at": wallet.last_checked_at.isoformat() if wallet.last_checked_at else None,
            "error": None,
        }
    except Exception as e:
        wallet.last_refresh_count = 0
        wallet.last_error_at = now
        wallet.last_error_message = str(e)
        _create_sync_event(
            db,
            wallet.address,
            status="error",
            error_message=str(e),
        )
        db.commit()
        return {
            "wallet": wallet.address,
            "fetched": 0,
            "inserted": 0,
            "stats": calculate_wallet_stats_snapshot(db, wallet.address),
            "last_checked_at": wallet.last_checked_at.isoformat() if wallet.last_checked_at else None,
            "error": str(e),
        }


def ingest_trades(db: Session, wallet_address: str) -> int:
    """Backward-compatible helper returning only the inserted trade count."""
    wallet = db.query(Wallet).filter(Wallet.address == wallet_address.strip().lower()).first()
    if not wallet:
        wallet = Wallet(address=wallet_address.strip().lower())
        db.add(wallet)
        db.commit()
        db.refresh(wallet)
    return refresh_wallet(db, wallet)["inserted"]


def refresh_all_wallets(db: Session) -> Dict[str, int]:
    """Refresh all tracked wallets and return inserted counts keyed by address."""
    results: Dict[str, int] = {}
    for wallet in db.query(Wallet).all():
        results[wallet.address] = refresh_wallet(db, wallet)["inserted"]
    return results
