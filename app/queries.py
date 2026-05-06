import re
from typing import Any, Dict, List, Optional

from sqlalchemy import case, desc, func, or_, select, tuple_
from sqlalchemy.orm import Query, Session

from app.formatting import WALLET_STALE_HOURS, parse_datetime_end, parse_datetime_start
from app.models import SyncEvent, Trade, Wallet
from datetime import datetime, timedelta, timezone

WALLET_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


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


def wallet_order_query(db: Session) -> Query:
    return db.query(Wallet).order_by(
        func.coalesce(Wallet.is_archived, 0).asc(),
        desc(func.coalesce(Wallet.is_pinned, 0)),
        desc(Wallet.created_at),
    )


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


def trade_pnl_summary(query: Query) -> Dict[str, Any]:
    row = query.order_by(False).with_entities(
        func.sum(case((Trade.side == "YES", Trade.price * Trade.size), else_=0)).label("yes_value"),
        func.sum(case((Trade.side == "NO", Trade.price * Trade.size), else_=0)).label("no_value"),
        func.sum(Trade.price * Trade.size).label("total_value"),
        func.sum(Trade.size).label("total_size"),
        func.count(Trade.id).label("trade_count"),
        func.sum(case((Trade.side == "YES", 1), else_=0)).label("yes_count"),
        func.sum(case((Trade.side == "NO", 1), else_=0)).label("no_count"),
    ).first()
    total_value = float(row.total_value or 0)
    total_size = float(row.total_size or 0)
    return {
        "yes_value": float(row.yes_value or 0),
        "no_value": float(row.no_value or 0),
        "total_value": total_value,
        "avg_price": total_value / total_size if total_size > 0 else 0.0,
        "trade_count": int(row.trade_count or 0),
        "yes_count": int(row.yes_count or 0),
        "no_count": int(row.no_count or 0),
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


def apply_wallet_search_to_trade_query(db: Session, query: Query, wallet_search: Optional[str]) -> Query:
    """Filter a Trade query to wallets whose address or label matches wallet_search."""
    if not wallet_search:
        return query
    term = f"%{wallet_search.lower()}%"
    matching = (
        db.query(Wallet.address)
        .filter(
            or_(
                func.lower(func.coalesce(Wallet.address, "")).like(term),
                func.lower(func.coalesce(Wallet.label, "")).like(term),
            )
        )
        .subquery()
    )
    return query.filter(Trade.wallet_address.in_(select(matching.c.address)))


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
