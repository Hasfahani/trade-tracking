# Downloads and saves Polymarket trades.
import hashlib
import logging
import re
import ssl
import time
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import MarketResolution, SyncEvent, Trade, Wallet
from app.settings import (
    POLYMARKET_CONNECT_TIMEOUT_SECONDS,
    POLYMARKET_POOL_TIMEOUT_SECONDS,
    POLYMARKET_READ_TIMEOUT_SECONDS,
    POLYMARKET_WRITE_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)

DATA_API_BASE = "https://data-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
_MAX_RETRY_ATTEMPTS = 3
_INITIAL_BACKOFF_SECONDS = 2.0
_MAX_TRADES_PAGE_SIZE = 1000
_MAX_TRADES_OFFSET = 3000

# Per-refresh cap on resolution lookups (one HTTP call each) so a trade refresh
# stays responsive. The backfill script sweeps everything without this cap.
_RESOLUTION_FETCH_LIMIT = 12
# Polymarket condition ids are 0x + 64 hex chars. Validate before any network
# call so malformed/test ids never trigger a request.
_CONDITION_ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


@lru_cache(maxsize=1)
def _polymarket_ssl_context() -> ssl.SSLContext:
    # Use the local machine's trust store so Windows-installed CA certs are honored.
    return ssl.create_default_context()


def _polymarket_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=POLYMARKET_CONNECT_TIMEOUT_SECONDS,
        read=POLYMARKET_READ_TIMEOUT_SECONDS,
        write=POLYMARKET_WRITE_TIMEOUT_SECONDS,
        pool=POLYMARKET_POOL_TIMEOUT_SECONDS,
    )


def fetch_trades_for_wallet(address: str, limit: int = 1000, fetch_all: bool = False) -> List[Dict[str, Any]]:
    """Fetch trades for a wallet from Polymarket public data API."""
    address = address.lower().strip()
    all_trades: List[Dict[str, Any]] = []
    limit = min(max(int(limit), 1), _MAX_TRADES_PAGE_SIZE)

    if fetch_all:
        offset = 0
        while True:
            batch = _fetch_trade_batch(address=address, limit=limit, offset=offset)
            if not batch:
                break
            all_trades.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
            if offset > _MAX_TRADES_OFFSET:
                logger.info(
                    "Reached Polymarket historical trade offset cap for wallet=%s after %d rows",
                    address,
                    len(all_trades),
                )
                break
        return all_trades

    return _fetch_trade_batch(address=address, limit=limit)


def _fetch_trade_batch(address: str, limit: int, offset: Optional[int] = None) -> List[Dict[str, Any]]:
    url = f"{DATA_API_BASE}/trades"
    params: Dict[str, Any] = {"user": address, "limit": limit}
    if offset is not None:
        params["offset"] = offset

    backoff = _INITIAL_BACKOFF_SECONDS
    for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=_polymarket_timeout(), verify=_polymarket_ssl_context()) as client:
                response = client.get(url, params=params)
        except httpx.TransportError:
            if attempt >= _MAX_RETRY_ATTEMPTS:
                raise
            logger.warning(
                "Polymarket transport error for wallet=%s offset=%s, attempt=%d/%d, sleeping=%.1fs",
                address,
                offset,
                attempt,
                _MAX_RETRY_ATTEMPTS,
                backoff,
                exc_info=True,
            )
            time.sleep(backoff)
            backoff *= 2
            continue

        status_code = getattr(response, "status_code", None)
        if status_code == 429:
            if attempt < _MAX_RETRY_ATTEMPTS:
                retry_after = float(response.headers.get("Retry-After", backoff))
                wait = min(retry_after, backoff)
                logger.warning(
                    "Rate limited by Polymarket API for wallet=%s, attempt=%d/%d, sleeping=%.1fs",
                    address, attempt, _MAX_RETRY_ATTEMPTS, wait,
                )
                time.sleep(wait)
                backoff *= 2
                continue
            response.raise_for_status()

        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return payload
        logger.warning("Unexpected trades payload for wallet=%s type=%s", address, type(payload).__name__)
        return []

    return []


def normalize_trade(raw: Dict[str, Any], wallet_address: str) -> Optional[Dict[str, Any]]:
    """Normalize external trade payload to the local schema."""
    try:
        condition_id = str(raw.get("conditionId") or "").strip()
        if not condition_id:
            return None

        price = float(raw.get("price") or 0)
        size = float(raw.get("size") or 0)
        if price <= 0 or size <= 0:
            return None

        market_title = str(raw.get("title") or "Unknown Market").strip() or "Unknown Market"

        side_raw = str(raw.get("side") or "").upper().strip()
        outcome = str(raw.get("outcome") or "").upper().strip()
        if side_raw == "BUY":
            side = "YES"
        elif side_raw == "SELL":
            side = "NO"
        elif outcome in {"YES", "NO"}:
            side = outcome
        else:
            logger.warning("Unknown side/outcome in trade payload: side=%r outcome=%r", side_raw, outcome)
            return None

        # The Yes/No token traded, independent of buy/sell. Needed for realized
        # PnL once the market resolves. None when the API omits a usable outcome.
        outcome_token = outcome if outcome in {"YES", "NO"} else None

        ts = raw.get("timestamp")
        if isinstance(ts, (int, float)):
            traded_at = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif isinstance(ts, str) and ts.strip():
            traded_at = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            traded_at = datetime.now(timezone.utc)

        external_trade_id = str(raw.get("id") or "").strip()
        if external_trade_id:
            trade_id = external_trade_id
        else:
            tx_hash = str(raw.get("transactionHash") or "").strip()
            asset = str(raw.get("asset") or "").strip()
            if not tx_hash:
                return None

            fingerprint = ":".join(
                [
                    tx_hash,
                    asset,
                    condition_id,
                    side,
                    f"{price:.8f}",
                    f"{size:.8f}",
                    traded_at.isoformat(),
                ]
            )
            trade_id = hashlib.sha256(fingerprint.encode()).hexdigest()[:24]

        return {
            "id": trade_id,
            "wallet_address": wallet_address.lower(),
            "condition_id": condition_id,
            "market_title": market_title,
            "side": side,
            "price": price,
            "size": size,
            "traded_at": traded_at,
            "outcome_token": outcome_token,
        }
    except Exception:
        logger.exception("Failed to normalize trade payload")
        return None


def calculate_wallet_stats_snapshot(db: Session, wallet_address: str) -> Dict[str, Any]:
    """Return a lightweight stats snapshot for a wallet after a refresh.

    Args:
        db: Active SQLAlchemy session.
        wallet_address: The 0x-prefixed wallet address.

    Returns:
        Dict with keys ``total_trades`` and ``last_trade_date`` (ISO string or ``None``).
    """
    row = (
        db.query(
            func.count(Trade.id).label("total_trades"),
            func.max(Trade.traded_at).label("last_trade_date"),
        )
        .filter(Trade.wallet_address == wallet_address)
        .first()
    )
    total_trades = int(row.total_trades or 0)

    return {
        "total_trades": total_trades,
        "last_trade_date": row.last_trade_date.isoformat() if row.last_trade_date else None,
    }


def find_duplicate_groups(db: Session, wallet_address: Optional[str] = None) -> List[Dict[str, Any]]:
    """Identify groups of semantically-duplicate trades in the database.

    Two trades are considered duplicates when they share the same wallet,
    condition ID, side, price, size, and traded_at timestamp.

    Args:
        db: Active SQLAlchemy session.
        wallet_address: If provided, restrict the scan to this wallet.

    Returns:
        List of dicts describing each duplicate group with a ``duplicate_count`` key.
    """
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
    """Remove duplicate trades, keeping the lowest-ID record in each group.

    Args:
        db: Active SQLAlchemy session.

    Returns:
        Number of trade rows deleted.
    """
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
    duplicate_count: int = 0,
    duration_ms: Optional[int] = None,
    error_message: Optional[str] = None,
) -> None:
    db.add(
        SyncEvent(
            wallet_address=wallet_address,
            status=status,
            fetched_count=fetched_count,
            inserted_count=inserted_count,
            duplicate_count=duplicate_count,
            duration_ms=duration_ms,
            error_message=error_message,
        )
    )


def _score_unscored_trades(db: Session, wallet_address: str) -> int:
    """Compute notable_score for the wallet's trades that have none yet.

    No-op when the model weights are not available. Returns the number of
    trades updated.
    """
    from app.ml.features import build_features_for_wallet
    from app.ml.model import get_signal_model

    model = get_signal_model()
    if model is None:
        return 0

    trades = (
        db.query(Trade)
        .filter(Trade.wallet_address == wallet_address)
        .order_by(Trade.traded_at.asc(), Trade.id.asc())
        .all()
    )
    X, _, trade_ids = build_features_for_wallet(trades)
    if not trade_ids:
        return 0

    scores = model.predict_full(X)
    trades_by_id = {trade.trade_id: trade for trade in trades}
    updated = 0
    for trade_id, score in zip(trade_ids, scores):
        trade = trades_by_id[trade_id]
        if trade.notable_score is None:
            trade.notable_score = float(score)
            updated += 1
    if updated:
        db.commit()
    return updated


def _backfill_outcome_tokens(db: Session, normalized: List[Dict[str, Any]], existing_ids: set) -> None:
    """Set outcome_token on already-stored trades that predate the column.

    Uses the payload we just fetched. Only two token values exist, so this is at
    most two UPDATE statements per chunk and only touches rows where the column
    is still NULL.
    """
    by_token: Dict[str, List[str]] = {"YES": [], "NO": []}
    for trade in normalized:
        token = trade.get("outcome_token")
        if trade["id"] in existing_ids and token in by_token:
            by_token[token].append(trade["id"])

    for token, trade_ids in by_token.items():
        for start in range(0, len(trade_ids), 500):
            chunk = trade_ids[start : start + 500]
            if not chunk:
                continue
            db.execute(
                update(Trade)
                .where(Trade.trade_id.in_(chunk), Trade.outcome_token.is_(None))
                .values(outcome_token=token)
            )


def parse_market_resolution(market: Any, condition_id: str) -> Optional[Dict[str, Any]]:
    """Parse a CLOB market payload into a resolution record (pure, no network).

    Returns a dict with condition_id, market_title, outcome (YES/NO/UNRESOLVED),
    and resolved_at, or None if the payload is unusable. A closed market with
    exactly one winning Yes/No token resolves to that outcome; anything else is
    treated as UNRESOLVED so we never fabricate a result.
    """
    if not isinstance(market, dict):
        return None
    market_title = str(market.get("question") or "").strip() or None
    resolved_at = None
    end_iso = market.get("end_date_iso") or market.get("endDate")
    if isinstance(end_iso, str) and end_iso.strip():
        try:
            resolved_at = datetime.fromisoformat(end_iso.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            resolved_at = None

    base = {"condition_id": condition_id, "market_title": market_title}
    if not market.get("closed"):
        return {**base, "outcome": "UNRESOLVED", "resolved_at": None}

    winners = [
        str(token.get("outcome") or "").upper()
        for token in (market.get("tokens") or [])
        if token.get("winner")
    ]
    winning = [w for w in winners if w in {"YES", "NO"}]
    if len(winning) == 1:
        return {**base, "outcome": winning[0], "resolved_at": resolved_at}
    # Closed but ambiguous (no/many winners, or non-binary) - stay honest.
    return {**base, "outcome": "UNRESOLVED", "resolved_at": resolved_at}


def fetch_market_resolution(condition_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single market's resolution from the Polymarket CLOB API.

    Returns a parsed resolution dict, or None on any network/format error or a
    non-conforming condition id. Never raises - resolution is best-effort.
    """
    condition_id = (condition_id or "").strip()
    if not _CONDITION_ID_RE.match(condition_id):
        return None
    url = f"{CLOB_API_BASE}/markets/{condition_id}"
    try:
        with httpx.Client(timeout=_polymarket_timeout(), verify=_polymarket_ssl_context()) as client:
            response = client.get(url)
        if response.status_code != 200:
            return None
        return parse_market_resolution(response.json(), condition_id)
    except Exception:
        logger.warning("Resolution fetch failed for condition=%s", condition_id, exc_info=True)
        return None


def upsert_market_resolution(db: Session, data: Dict[str, Any]) -> MarketResolution:
    """Insert or update a market_resolutions row from a parsed resolution dict."""
    row = db.get(MarketResolution, data["condition_id"])
    if row is None:
        row = MarketResolution(condition_id=data["condition_id"])
        db.add(row)
    if data.get("market_title"):
        row.market_title = data["market_title"]
    row.outcome = data["outcome"]
    row.resolved_at = data.get("resolved_at")
    row.checked_at = datetime.now(timezone.utc).replace(tzinfo=None)
    return row


def refresh_resolutions_for_wallet(db: Session, wallet_address: str, max_markets: int = _RESOLUTION_FETCH_LIMIT) -> int:
    """Fetch resolutions for a wallet's not-yet-resolved markets (newest first).

    Bounded by ``max_markets`` so a refresh stays responsive. Returns the number
    of markets newly resolved (YES/NO). Best-effort: individual fetch failures
    are skipped.
    """
    already_resolved = {
        cid for (cid,) in db.query(MarketResolution.condition_id)
        .filter(MarketResolution.outcome.in_(("YES", "NO"))).all()
    }
    market_rows = (
        db.query(Trade.condition_id, func.max(Trade.traded_at).label("last_traded"))
        .filter(Trade.wallet_address == wallet_address)
        .group_by(Trade.condition_id)
        .order_by(func.max(Trade.traded_at).desc())
        .all()
    )
    candidates = [
        cid for (cid, _last) in market_rows
        if cid not in already_resolved and _CONDITION_ID_RE.match(cid or "")
    ][:max_markets]

    resolved = 0
    for condition_id in candidates:
        data = fetch_market_resolution(condition_id)
        if data is None:
            continue
        upsert_market_resolution(db, data)
        if data["outcome"] in ("YES", "NO"):
            resolved += 1
    if candidates:
        db.commit()
    return resolved


def refresh_wallet(
    db: Session,
    wallet: Wallet,
    *,
    fetch_all: bool = False,
    limit: int = 1000,
    fetch_resolutions: bool = True,
) -> Dict[str, Any]:
    """Refresh a wallet and store sync status in SQLite."""
    started_at = datetime.now(timezone.utc)
    wallet.last_checked_at = started_at
    wallet.last_error_at = None
    wallet.last_error_message = None

    try:
        raw_trades = fetch_trades_for_wallet(wallet.address, limit=limit, fetch_all=fetch_all)
        normalized_rows = [trade for trade in (normalize_trade(raw, wallet.address) for raw in raw_trades) if trade]
        normalized_by_id: Dict[str, Dict[str, Any]] = {}
        for trade in normalized_rows:
            normalized_by_id.setdefault(trade["id"], trade)
        normalized = list(normalized_by_id.values())

        inserted = 0
        if normalized:
            all_ids = [t["id"] for t in normalized]
            existing_ids = {
                row[0]
                for row in db.execute(select(Trade.trade_id).where(Trade.trade_id.in_(all_ids)))
            }
            new_trades = [t for t in normalized if t["id"] not in existing_ids]
            if new_trades:
                db.execute(
                    Trade.__table__.insert(),
                    [
                        {
                            "trade_id": t["id"],
                            "wallet_address": t["wallet_address"],
                            "condition_id": t["condition_id"],
                            "market_title": t["market_title"],
                            "side": t["side"],
                            "price": t["price"],
                            "size": t["size"],
                            "traded_at": t["traded_at"],
                            "outcome_token": t["outcome_token"],
                        }
                        for t in new_trades
                    ],
                )
            inserted = len(new_trades)

            # Backfill outcome_token on trades ingested before that column
            # existed, using the freshly-fetched payload we already have.
            _backfill_outcome_tokens(db, normalized, existing_ids)

        duplicate_count = len(raw_trades) - inserted
        status = "no_new" if inserted == 0 else "success"
        wallet.last_refresh_status = status
        wallet.last_refresh_count = inserted
        _create_sync_event(
            db,
            wallet.address,
            status=status,
            fetched_count=len(raw_trades),
            inserted_count=inserted,
            duplicate_count=duplicate_count,
            duration_ms=max(int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000), 0),
        )
        db.commit()

        if inserted:
            try:
                _score_unscored_trades(db, wallet.address)
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                logger.warning(
                    "Notable scoring failed for wallet=%s - refresh unaffected", wallet.address, exc_info=True
                )

        # Best-effort: fetch resolved outcomes for this wallet's markets. Network
        # failures here must never fail the refresh (trades are already saved).
        if fetch_resolutions:
            try:
                refresh_resolutions_for_wallet(db, wallet.address)
            except Exception:
                try:
                    db.rollback()
                except Exception:
                    pass
                logger.warning(
                    "Resolution fetch failed for wallet=%s - refresh unaffected", wallet.address, exc_info=True
                )

        stats = calculate_wallet_stats_snapshot(db, wallet.address)
        return {
            "wallet": wallet.address,
            "status": status,
            "fetched": len(raw_trades),
            "inserted": inserted,
            "duplicates": duplicate_count,
            "stats": stats,
            "last_checked_at": wallet.last_checked_at.isoformat() if wallet.last_checked_at else None,
            "error": None,
        }
    except httpx.TimeoutException as exc:
        error_msg = "API unreachable - connection timed out"
        logger.warning("Wallet refresh timeout for %s: %s", wallet.address, exc)
        return _handle_refresh_error(db, wallet, started_at, error_msg)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            error_msg = "Rate limited - try again later"
        else:
            error_msg = f"API error {exc.response.status_code}"
        logger.warning("Wallet refresh HTTP error for %s: %s", wallet.address, exc)
        return _handle_refresh_error(db, wallet, started_at, error_msg)
    except (ValueError, KeyError, TypeError) as exc:
        error_msg = "Unexpected API response format"
        logger.exception("Wallet refresh parse error for %s", wallet.address)
        return _handle_refresh_error(db, wallet, started_at, error_msg)
    except Exception as exc:
        logger.exception("Wallet refresh failed for %s", wallet.address)
        return _handle_refresh_error(db, wallet, started_at, str(exc))


def _handle_refresh_error(db, wallet, started_at, error_msg: str) -> dict:
    """Record a refresh error and return the standard error result dict."""
    wallet.last_refresh_status = "error"
    wallet.last_refresh_count = 0
    wallet.last_error_at = started_at
    wallet.last_error_message = error_msg
    _create_sync_event(
        db,
        wallet.address,
        status="error",
        fetched_count=0,
        inserted_count=0,
        duplicate_count=0,
        duration_ms=max(int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000), 0),
        error_message=error_msg,
    )
    db.commit()
    return {
        "wallet": wallet.address,
        "status": "error",
        "fetched": 0,
        "inserted": 0,
        "duplicates": 0,
        "stats": calculate_wallet_stats_snapshot(db, wallet.address),
        "last_checked_at": wallet.last_checked_at.isoformat() if wallet.last_checked_at else None,
        "error": error_msg,
    }


def ingest_trades(db: Session, wallet_address: str) -> int:
    """Backward-compatible helper returning only inserted count."""
    wallet = db.query(Wallet).filter(Wallet.address == wallet_address.strip().lower()).first()
    if not wallet:
        wallet = Wallet(address=wallet_address.strip().lower())
        db.add(wallet)
        db.commit()
        db.refresh(wallet)
    return int(refresh_wallet(db, wallet)["inserted"])
