from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from app.formatting import sync_status_class
from app.models import SyncEvent, Trade, Wallet

_LARGE_TRADE_THRESHOLD = 200.0
_SPIKE_COUNT = 3
_SPIKE_WINDOW_MINUTES = 10


def _short_address(address: str) -> str:
    # Local import avoided by duplicating the tiny helper inline.
    if len(address) <= 14:
        return address
    return f"{address[:8]}...{address[-6:]}"


def detect_interesting_activity(db: Session) -> List[Dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    recent = (
        db.query(Trade)
        .filter(Trade.traded_at >= cutoff)
        .order_by(Trade.traded_at.desc())
        .all()
    )

    wallet_map: Dict[str, Optional[str]] = {row.address: row.label or None for row in db.query(Wallet.address, Wallet.label).all()}

    def _label(address: str) -> str:
        return wallet_map.get(address) or _short_address(address)

    events: List[Dict[str, Any]] = []

    # A. Large single trades
    for trade in recent:
        value = trade.price * trade.size
        if value >= _LARGE_TRADE_THRESHOLD:
            events.append({
                "type": "large_trade",
                "wallet": trade.wallet_address,
                "label": _label(trade.wallet_address),
                "market": trade.market_title or trade.condition_id,
                "value": value,
                "timestamp": trade.traded_at,
                "trade_id": trade.trade_id,
            })

    # B. Activity spikes — 3+ trades within a 10-minute sliding window per wallet
    by_wallet: Dict[str, List[datetime]] = {}
    for trade in recent:
        by_wallet.setdefault(trade.wallet_address, []).append(trade.traded_at)

    seen_spike: set = set()
    window = timedelta(minutes=_SPIKE_WINDOW_MINUTES)
    for address, timestamps in by_wallet.items():
        timestamps.sort(reverse=True)
        for i in range(len(timestamps) - _SPIKE_COUNT + 1):
            newest, oldest = timestamps[i], timestamps[i + _SPIKE_COUNT - 1]
            if (newest - oldest) <= window and address not in seen_spike:
                seen_spike.add(address)
                events.append({
                    "type": "activity_spike",
                    "wallet": address,
                    "label": _label(address),
                    "count": len([t for t in timestamps if (newest - t) <= window]),
                    "time_window": f"{_SPIKE_WINDOW_MINUTES}m",
                    "timestamp": newest,
                })
                break

    # C. First-ever trade by this wallet in a given market, within the cutoff window
    condition_pairs = {(t.wallet_address, t.condition_id) for t in recent}
    earliest_by_pair: Dict[tuple, datetime] = {}
    if condition_pairs:
        rows = (
            db.query(
                Trade.wallet_address,
                Trade.condition_id,
                func.min(Trade.traded_at).label("earliest_traded_at"),
            )
            .filter(tuple_(Trade.wallet_address, Trade.condition_id).in_(condition_pairs))
            .group_by(Trade.wallet_address, Trade.condition_id)
            .all()
        )
        earliest_by_pair = {(r.wallet_address, r.condition_id): r.earliest_traded_at for r in rows}

    for address, condition_id in condition_pairs:
        earliest = earliest_by_pair.get((address, condition_id))
        if earliest and earliest >= cutoff.replace(tzinfo=None):
            trade_for_market = next(
                (t for t in recent if t.wallet_address == address and t.condition_id == condition_id),
                None,
            )
            if trade_for_market:
                events.append({
                    "type": "new_market",
                    "wallet": address,
                    "label": _label(address),
                    "market": trade_for_market.market_title or condition_id,
                    "timestamp": earliest,
                    "trade_id": trade_for_market.trade_id,
                })

    def _ts(e: Dict[str, Any]) -> float:
        ts = e.get("timestamp")
        if ts is None:
            return 0.0
        if hasattr(ts, "tzinfo") and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()

    events.sort(key=_ts, reverse=True)
    return events[:10]


def get_wallet_intelligence_summary(db: Session, wallet_address: str) -> Dict[str, Any]:
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    trade_value = Trade.price * Trade.size

    total_row = db.query(
        func.count(Trade.id).label("total_trades"),
        func.avg(trade_value).label("average_trade_size"),
        func.count(func.distinct(Trade.condition_id)).label("total_markets_traded"),
    ).filter(Trade.wallet_address == wallet_address).first()

    recent_row = db.query(
        func.count(Trade.id).label("trades_last_24h"),
        func.sum(trade_value).label("total_value_last_24h"),
        func.count(func.distinct(Trade.condition_id)).label("markets_traded_last_24h"),
    ).filter(Trade.wallet_address == wallet_address, Trade.traded_at >= cutoff).first()

    trades_last_24h = int(recent_row.trades_last_24h or 0)
    total_value_last_24h = float(recent_row.total_value_last_24h or 0)
    markets_traded_last_24h = int(recent_row.markets_traded_last_24h or 0)

    if trades_last_24h == 0:
        activity_level, activity_tone = "Inactive", ""
        intelligence_text = "This wallet is currently inactive."
    elif trades_last_24h <= 2:
        activity_level, activity_tone = "Low", "info"
        intelligence_text = "This wallet has low recent activity with small trade volume."
    elif trades_last_24h >= 10:
        activity_level, activity_tone = "High", "success"
        intelligence_text = "This wallet is highly active in the last 24 hours and may be worth monitoring."
    else:
        activity_level, activity_tone = "Medium", "warning"
        if markets_traded_last_24h > 1 and total_value_last_24h >= 100:
            intelligence_text = "This wallet recently traded multiple markets with significant volume."
        else:
            intelligence_text = "This wallet has moderate recent activity in the last 24 hours."

    return {
        "activity_level": activity_level,
        "trades_last_24h": trades_last_24h,
        "total_value_last_24h": total_value_last_24h,
        "average_trade_size": float(total_row.average_trade_size or 0),
        "total_markets_traded": int(total_row.total_markets_traded or 0),
        "markets_traded_last_24h": markets_traded_last_24h,
        "intelligence_text": intelligence_text,
        "activity_tone": activity_tone,
        "total_trades": int(total_row.total_trades or 0),
    }


def build_wallet_activity_timeline(db: Session, wallet_address: str, limit: int = 12) -> List[Dict[str, Any]]:
    trade_events = [
        {
            "kind": "trade",
            "timestamp": trade.traded_at,
            "title": trade.market_title or trade.condition_id,
            "detail": f"{trade.side} | ${trade.price:.4f} | {trade.size:.2f}",
            "href": f"/trades/{trade.trade_id}",
            "tone": "success" if trade.side == "YES" else "danger",
        }
        for trade in (
            db.query(Trade)
            .filter(Trade.wallet_address == wallet_address)
            .order_by(Trade.traded_at.desc())
            .limit(limit)
            .all()
        )
    ]

    sync_events = [
        {
            "kind": "sync",
            "timestamp": event.created_at,
            "title": f"Refresh {event.status or 'unknown'}",
            "detail": (
                f"Fetched {event.fetched_count or 0}, inserted {event.inserted_count or 0}, "
                f"duplicates {event.duplicate_count or 0}"
            ),
            "href": f"/admin/sync-status?wallet_search={wallet_address}",
            "tone": sync_status_class(event.status),
            "error_message": event.error_message,
        }
        for event in (
            db.query(SyncEvent)
            .filter(SyncEvent.wallet_address == wallet_address)
            .order_by(SyncEvent.created_at.desc())
            .limit(limit)
            .all()
        )
    ]

    def _key(item: Dict[str, Any]) -> float:
        value = item.get("timestamp")
        if value is None:
            return 0.0
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    timeline = trade_events + sync_events
    timeline.sort(key=_key, reverse=True)
    return timeline[:limit]


def build_activity_heatmap(db: Session, wallet_address: Optional[str] = None, days: int = 7) -> List[Dict[str, Any]]:
    """Return a list of daily trade-count buckets for the past `days` days.

    Pass wallet_address to scope to a single wallet; omit for all wallets.
    """
    today = datetime.now(timezone.utc).date()
    cutoff_day = today - timedelta(days=days - 1)
    cutoff = datetime(cutoff_day.year, cutoff_day.month, cutoff_day.day)

    query = (
        db.query(func.date(Trade.traded_at).label("trade_day"), func.count(Trade.id).label("trade_count"))
        .filter(Trade.traded_at >= cutoff)
        .group_by(func.date(Trade.traded_at))
        .order_by(func.date(Trade.traded_at).asc())
    )
    if wallet_address:
        query = query.filter(Trade.wallet_address == wallet_address)

    daily_map = {str(row.trade_day): int(row.trade_count or 0) for row in query.all()}
    max_count = max(daily_map.values(), default=0)

    return [
        {
            "label": (today - timedelta(days=offset)).strftime("%a"),
            "date": (today - timedelta(days=offset)).isoformat(),
            "count": daily_map.get((today - timedelta(days=offset)).isoformat(), 0),
            "bar_pct": (
                round((daily_map.get((today - timedelta(days=offset)).isoformat(), 0) / max_count) * 100)
                if max_count
                else 0
            ),
        }
        for offset in range(days - 1, -1, -1)
    ]


def build_top_markets(db: Session, wallet_address: Optional[str] = None, limit: int = 5) -> List[Dict[str, Any]]:
    """Return the top markets by total trade value, optionally scoped to one wallet."""
    trade_value = Trade.price * Trade.size
    query = db.query(
        Trade.condition_id,
        func.max(Trade.market_title).label("market_title"),
        func.count(Trade.id).label("trade_count"),
        func.sum(trade_value).label("total_value"),
    )
    if wallet_address:
        query = query.filter(Trade.wallet_address == wallet_address)
    rows = query.group_by(Trade.condition_id).order_by(func.sum(trade_value).desc()).limit(limit).all()

    top_value = float(rows[0].total_value or 0) if rows else 0
    return [
        {
            "condition_id": row.condition_id,
            "market": row.market_title or row.condition_id,
            "trade_count": int(row.trade_count or 0),
            "total_value": float(row.total_value or 0),
            "bar_pct": round((float(row.total_value or 0) / top_value) * 100) if top_value else 0,
        }
        for row in rows
    ]
