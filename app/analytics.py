from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, text, tuple_
from sqlalchemy.orm import Query, Session

from app.formatting import short_address as _short_address, sync_status_class
from app.models import SyncEvent, Trade, Wallet

_LARGE_TRADE_THRESHOLD = 200.0
_SPIKE_COUNT = 3
_SPIKE_WINDOW_MINUTES = 10


def detect_interesting_activity(db: Session) -> List[Dict[str, Any]]:
    """Return up to 10 notable trade events from the last 24 hours.

    Categories:
    - large_trade: single trade whose price × size >= _LARGE_TRADE_THRESHOLD.
    - activity_spike: wallet with _SPIKE_COUNT+ trades within _SPIKE_WINDOW_MINUTES.
    - new_market: wallet's first-ever trade on a condition entered within the window.

    Queries are SQL-only; no full table scans in Python.
    """
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
    trade_value = Trade.price * Trade.size

    wallet_map: Dict[str, Optional[str]] = {
        row.address: row.label or None
        for row in db.query(Wallet.address, Wallet.label).all()
    }

    def _label(address: str) -> str:
        return wallet_map.get(address) or _short_address(address)

    events: List[Dict[str, Any]] = []

    # --- A. Large single trades (SQL-filtered) ---
    large_trades = (
        db.query(Trade)
        .filter(Trade.traded_at >= cutoff, trade_value >= _LARGE_TRADE_THRESHOLD)
        .order_by(Trade.traded_at.desc())
        .limit(20)
        .all()
    )
    for trade in large_trades:
        events.append({
            "type": "large_trade",
            "wallet": trade.wallet_address,
            "label": _label(trade.wallet_address),
            "market": trade.market_title or trade.condition_id,
            "value": trade.price * trade.size,
            "timestamp": trade.traded_at,
            "trade_id": trade.trade_id,
        })

    # --- B. Activity spikes — wallets with many trades in a short window ---
    # Fetch per-wallet trade counts in the window; then check window tightness
    # for the top candidates only (avoids loading all trades into memory).
    spike_candidates = (
        db.query(
            Trade.wallet_address,
            func.count(Trade.id).label("cnt"),
        )
        .filter(Trade.traded_at >= cutoff)
        .group_by(Trade.wallet_address)
        .having(func.count(Trade.id) >= _SPIKE_COUNT)
        .order_by(func.count(Trade.id).desc())
        .limit(20)
        .all()
    )

    spike_window = timedelta(minutes=_SPIKE_WINDOW_MINUTES)
    seen_spike = set()
    for row in spike_candidates:
        if row.wallet_address in seen_spike:
            continue
        timestamps = [
            t.traded_at
            for t in (
                db.query(Trade.traded_at)
                .filter(Trade.wallet_address == row.wallet_address, Trade.traded_at >= cutoff)
                .order_by(Trade.traded_at.desc())
                .limit(50)
                .all()
            )
        ]
        timestamps.sort(reverse=True)
        for i in range(len(timestamps) - _SPIKE_COUNT + 1):
            newest, oldest = timestamps[i], timestamps[i + _SPIKE_COUNT - 1]
            if (newest - oldest) <= spike_window:
                seen_spike.add(row.wallet_address)
                window_count = sum(1 for t in timestamps if (newest - t) <= spike_window)
                events.append({
                    "type": "activity_spike",
                    "wallet": row.wallet_address,
                    "label": _label(row.wallet_address),
                    "count": window_count,
                    "time_window": f"{_SPIKE_WINDOW_MINUTES}m",
                    "timestamp": newest,
                })
                break

    # --- C. New market entries in the window ---
    # Find the earliest trade per (wallet, condition) globally, then check
    # if that earliest trade falls within the cutoff window.
    recent_pairs_rows = (
        db.query(Trade.wallet_address, Trade.condition_id)
        .filter(Trade.traded_at >= cutoff)
        .distinct()
        .all()
    )
    condition_pairs = {(r.wallet_address, r.condition_id) for r in recent_pairs_rows}

    if condition_pairs:
        earliest_rows = (
            db.query(
                Trade.wallet_address,
                Trade.condition_id,
                func.min(Trade.traded_at).label("earliest"),
            )
            .filter(tuple_(Trade.wallet_address, Trade.condition_id).in_(condition_pairs))
            .group_by(Trade.wallet_address, Trade.condition_id)
            .all()
        )
        # Fetch representative trades for new-market entries in one query
        new_pairs = {
            (r.wallet_address, r.condition_id)
            for r in earliest_rows
            if r.earliest and r.earliest >= cutoff
        }
        if new_pairs:
            # Build earliest-timestamp lookup for the pairs that qualify
            earliest_map = {
                (r.wallet_address, r.condition_id): r.earliest
                for r in earliest_rows
                if (r.wallet_address, r.condition_id) in new_pairs and r.earliest
            }
            # Fetch all representative trades in one batch query
            if earliest_map:
                batch_trades = (
                    db.query(Trade)
                    .filter(tuple_(Trade.wallet_address, Trade.condition_id).in_(list(new_pairs)))
                    .order_by(Trade.traded_at.asc())
                    .all()
                )
                rep_trades: Dict[tuple, Trade] = {}
                for trade in batch_trades:
                    key = (trade.wallet_address, trade.condition_id)
                    earliest = earliest_map.get(key)
                    if earliest and trade.traded_at == earliest and key not in rep_trades:
                        rep_trades[key] = trade

                for key, trade in rep_trades.items():
                    events.append({
                        "type": "new_market",
                        "wallet": key[0],
                        "label": _label(key[0]),
                        "market": trade.market_title or key[1],
                        "timestamp": earliest_map[key],
                        "trade_id": trade.trade_id,
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
    """Return an activity intelligence summary for a single wallet."""
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
        intelligence_text = "Low recent activity — minimal trade volume in the last day."
    elif trades_last_24h >= 10:
        activity_level, activity_tone = "High", "success"
        intelligence_text = f"Highly active: {trades_last_24h} trades in 24h across {markets_traded_last_24h} market{'s' if markets_traded_last_24h != 1 else ''}. Worth monitoring."
    else:
        activity_level, activity_tone = "Medium", "warning"
        if markets_traded_last_24h > 1 and total_value_last_24h >= 100:
            intelligence_text = f"Active across {markets_traded_last_24h} markets with ${total_value_last_24h:,.2f} in stored trade value today."
        else:
            intelligence_text = "Moderate recent activity in the last 24 hours."

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
    """Return a merged, newest-first timeline of trades and sync events for a wallet."""
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
    """Return daily trade-count buckets for the past ``days`` days."""
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
                if max_count else 0
            ),
        }
        for offset in range(days - 1, -1, -1)
    ]


def build_filtered_top_markets(query: Query, limit: int = 5) -> List[Dict[str, Any]]:
    """Return top markets by trade value from a pre-filtered Trade query."""
    trade_value = Trade.price * Trade.size
    rows = (
        query.order_by(False)
        .with_entities(
            Trade.condition_id,
            func.max(Trade.market_title).label("market_title"),
            func.count(Trade.id).label("trade_count"),
            func.sum(trade_value).label("total_value"),
        )
        .group_by(Trade.condition_id)
        .order_by(func.sum(trade_value).desc())
        .limit(limit)
        .all()
    )
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


def build_filtered_top_wallets(query: Query, limit: int = 5) -> List[Dict[str, Any]]:
    """Return top wallets by trade value from a pre-filtered Trade query."""
    trade_value = Trade.price * Trade.size
    rows = (
        query.order_by(False)
        .with_entities(
            Trade.wallet_address,
            func.count(Trade.id).label("trade_count"),
            func.sum(trade_value).label("total_value"),
        )
        .group_by(Trade.wallet_address)
        .order_by(func.sum(trade_value).desc())
        .limit(limit)
        .all()
    )
    top_value = float(rows[0].total_value or 0) if rows else 0
    return [
        {
            "address": row.wallet_address,
            "trade_count": int(row.trade_count or 0),
            "total_value": float(row.total_value or 0),
            "bar_pct": round((float(row.total_value or 0) / top_value) * 100) if top_value else 0,
        }
        for row in rows
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
