import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import case, desc, func, or_
from sqlalchemy.orm import Query, Session

from app.models import SyncEvent, Trade, Wallet

WALLET_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
WALLET_STALE_HOURS = 24


def wallet_order_query(db: Session) -> Query:
    return db.query(Wallet).order_by(
        func.coalesce(Wallet.is_archived, 0).asc(),
        desc(func.coalesce(Wallet.is_pinned, 0)),
        desc(Wallet.created_at),
    )


def short_address(address: str) -> str:
    if len(address) <= 14:
        return address
    return f"{address[:8]}...{address[-6:]}"


def validate_wallet_address(address: str) -> Optional[str]:
    candidate = (address or "").strip().lower()
    if not candidate:
        return "Wallet address is required."
    if not WALLET_ADDRESS_RE.match(candidate):
        return "Wallet address must be a valid 42-character hex address starting with 0x."
    return None


def normalize_tags(tags: Optional[str]) -> str:
    raw = tags or ""
    items: List[str] = []
    seen = set()
    for piece in re.split(r"[,|\n]+", raw):
        tag = piece.strip()
        if not tag:
            continue
        lowered = tag.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        items.append(tag)
    return ", ".join(items)


def tag_list(tags: Optional[str]) -> List[str]:
    if not tags:
        return []
    return [tag.strip() for tag in tags.split(",") if tag.strip()]


def wallet_status_tone(wallet: Wallet) -> str:
    if wallet.last_refresh_status == "error":
        return "danger"
    if not wallet.last_checked_at:
        return "warning"
    now = datetime.now(timezone.utc)
    checked_at = wallet.last_checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    age = now - checked_at
    if age <= timedelta(hours=WALLET_STALE_HOURS):
        return "success"
    return "warning"


def wallet_freshness_label(wallet: Wallet) -> str:
    if wallet.last_refresh_status == "error":
        return "Failed"
    if not wallet.last_checked_at:
        return "Never refreshed"
    now = datetime.now(timezone.utc)
    checked_at = wallet.last_checked_at
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    age = now - checked_at
    if age <= timedelta(hours=WALLET_STALE_HOURS):
        return "Fresh"
    return "Stale"


def duration_label(duration_ms: Optional[int]) -> str:
    if duration_ms is None:
        return "-"
    if duration_ms < 1000:
        return f"{duration_ms} ms"
    return f"{duration_ms / 1000:.1f} s"


def date_preset_range(preset: Optional[str]) -> Dict[str, Optional[str]]:
    today = datetime.now().date()
    if preset == "today":
        value = today.isoformat()
        return {"date_from": value, "date_to": value}
    if preset == "7d":
        return {"date_from": (today - timedelta(days=6)).isoformat(), "date_to": today.isoformat()}
    if preset == "30d":
        return {"date_from": (today - timedelta(days=29)).isoformat(), "date_to": today.isoformat()}
    return {"date_from": None, "date_to": None}


def parse_datetime_start(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_datetime_end(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text) + timedelta(days=1)
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def pagination_meta(page: int, page_size: int, total_items: int) -> Dict[str, int]:
    if total_items <= 0:
        return {"start": 0, "end": 0}
    start = ((page - 1) * page_size) + 1
    end = min(total_items, page * page_size)
    return {"start": start, "end": end}


def apply_trade_filters(
    query: Query,
    *,
    wallet_address: Optional[str] = None,
    side: Optional[str] = None,
    market_search: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> Query:
    if wallet_address:
        query = query.filter(Trade.wallet_address == wallet_address)

    if side in {"YES", "NO"}:
        query = query.filter(Trade.side == side)

    if market_search:
        term = f"%{market_search.strip()}%"
        query = query.filter(
            or_(
                Trade.market_title.ilike(term),
                Trade.condition_id.ilike(term),
                Trade.trade_id.ilike(term),
            )
        )

    start_at = parse_datetime_start(date_from)
    if start_at is not None:
        query = query.filter(Trade.traded_at >= start_at)

    end_at = parse_datetime_end(date_to)
    if end_at is not None:
        query = query.filter(Trade.traded_at < end_at)

    return query


def sorted_trade_query(query: Query, sort_by: str) -> Query:
    if sort_by == "time_asc":
        return query.order_by(Trade.traded_at.asc())
    if sort_by == "size_desc":
        return query.order_by(Trade.size.desc(), Trade.traded_at.desc())
    if sort_by == "value_desc":
        return query.order_by((Trade.price * Trade.size).desc(), Trade.traded_at.desc())
    return query.order_by(Trade.traded_at.desc())


def trade_pnl_summary(query: Query) -> Dict[str, float]:
    row = query.with_entities(
        func.sum(case((Trade.side == "YES", Trade.price * Trade.size), else_=0)).label("yes_value"),
        func.sum(case((Trade.side == "NO", Trade.price * Trade.size), else_=0)).label("no_value"),
        func.sum(Trade.price * Trade.size).label("total_value"),
        func.avg(Trade.price).label("avg_price"),
        func.count(Trade.id).label("trade_count"),
    ).first()
    return {
        "yes_value": float(row.yes_value or 0),
        "no_value": float(row.no_value or 0),
        "total_value": float(row.total_value or 0),
        "avg_price": float(row.avg_price or 0),
        "trade_count": int(row.trade_count or 0),
    }


def wallet_stats_map(db: Session, wallet_addresses: Optional[List[str]] = None) -> Dict[str, Dict[str, Any]]:
    query = db.query(
        Trade.wallet_address,
        func.count(Trade.id).label("trade_count"),
        func.max(Trade.traded_at).label("last_trade_at"),
    ).group_by(Trade.wallet_address)
    if wallet_addresses:
        query = query.filter(Trade.wallet_address.in_(wallet_addresses))

    stats_map: Dict[str, Dict[str, Any]] = {}
    for row in query.all():
        stats_map[row.wallet_address] = {
            "trade_count": int(row.trade_count or 0),
            "last_trade_at": row.last_trade_at,
        }
    return stats_map


def _wallet_search_filter(query: Query, wallet_search: Optional[str]) -> Query:
    term = (wallet_search or "").strip().lower()
    if not term:
        return query
    like_term = f"%{term}%"
    return query.filter(
        or_(
            func.lower(func.coalesce(Wallet.address, "")).like(like_term),
            func.lower(func.coalesce(Wallet.label, "")).like(like_term),
            func.lower(func.coalesce(Wallet.tags, "")).like(like_term),
            func.lower(func.coalesce(Wallet.notes, "")).like(like_term),
        )
    )


def build_wallet_query(
    db: Session,
    *,
    wallet_search: Optional[str] = None,
    status_filter: Optional[str] = None,
    include_archived: bool = False,
) -> Query:
    query = _wallet_search_filter(wallet_order_query(db), wallet_search)

    if status_filter == "archived":
        query = query.filter(func.coalesce(Wallet.is_archived, 0) == 1)
    elif not include_archived:
        query = query.filter(func.coalesce(Wallet.is_archived, 0) == 0)

    if status_filter == "active":
        query = query.filter(func.coalesce(Wallet.is_archived, 0) == 0)
    elif status_filter == "pinned":
        query = query.filter(func.coalesce(Wallet.is_pinned, 0) == 1)
    elif status_filter == "failed":
        query = query.filter(Wallet.last_refresh_status == "error")
    elif status_filter == "fresh":
        threshold = datetime.now(timezone.utc) - timedelta(hours=WALLET_STALE_HOURS)
        query = query.filter(Wallet.last_checked_at.is_not(None), Wallet.last_checked_at >= threshold)
    elif status_filter == "stale":
        threshold = datetime.now(timezone.utc) - timedelta(hours=WALLET_STALE_HOURS)
        query = query.filter(
            Wallet.last_refresh_status != "error",
            or_(Wallet.last_checked_at.is_(None), Wallet.last_checked_at < threshold),
        )
    return query


def wallet_summary_counts(
    db: Session,
    *,
    wallet_search: Optional[str] = None,
    status_filter: Optional[str] = None,
    include_archived: bool = False,
) -> Dict[str, int]:
    wallet_ids_query = build_wallet_query(
        db,
        wallet_search=wallet_search,
        status_filter=status_filter,
        include_archived=include_archived,
    ).with_entities(Wallet.id)
    wallet_ids_subquery = wallet_ids_query.subquery()

    summary_row = db.query(
        func.count(Wallet.id).label("wallet_count"),
        func.sum(case((func.coalesce(Wallet.is_pinned, 0) == 1, 1), else_=0)).label("pinned_count"),
        func.sum(case((func.coalesce(Wallet.is_archived, 0) == 1, 1), else_=0)).label("archived_count"),
        func.sum(case((Wallet.last_checked_at.is_not(None), 1), else_=0)).label("refreshed_count"),
        func.sum(case((Wallet.last_refresh_status == "error", 1), else_=0)).label("error_count"),
    ).join(wallet_ids_subquery, wallet_ids_subquery.c.id == Wallet.id).one()

    trade_count = int(
        db.query(func.count(Trade.id))
        .filter(Trade.wallet_address.in_(db.query(Wallet.address).join(wallet_ids_subquery, wallet_ids_subquery.c.id == Wallet.id)))
        .scalar()
        or 0
    )

    return {
        "wallet_count": int(summary_row.wallet_count or 0),
        "pinned_count": int(summary_row.pinned_count or 0),
        "archived_count": int(summary_row.archived_count or 0),
        "refreshed_count": int(summary_row.refreshed_count or 0),
        "error_count": int(summary_row.error_count or 0),
        "trade_count": trade_count,
    }


def active_wallets(wallets: List[Wallet]) -> List[Wallet]:
    return [wallet for wallet in wallets if not wallet.is_archived]


def sync_status_class(status: Optional[str]) -> str:
    if status == "error":
        return "danger"
    if status == "no_new":
        return "warning"
    if status == "success":
        return "success"
    return "info"


def filter_sync_events(
    query: Query,
    *,
    wallet_search: Optional[str] = None,
    status: Optional[str] = None,
    error_only: bool = False,
) -> Query:
    if wallet_search:
        query = query.filter(func.lower(func.coalesce(SyncEvent.wallet_address, "")).like(f"%{wallet_search.lower()}%"))
    if status:
        query = query.filter(SyncEvent.status == status)
    if error_only:
        query = query.filter(SyncEvent.status == "error")
    return query


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

    def _timeline_key(item: Dict[str, Any]) -> float:
        value = item.get("timestamp")
        if value is None:
            return 0.0
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()

    timeline = trade_events + sync_events
    timeline.sort(key=_timeline_key, reverse=True)
    return timeline[:limit]
