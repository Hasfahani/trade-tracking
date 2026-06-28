# Summary: Discovers active Polymarket wallets to grow the tracked universe.
# Details: It supports the FastAPI backend by sweeping the public trade feed and
# proposing new wallets to watch, so the ML pipeline trains on many traders
# instead of a handful.
"""Wallet discovery from Polymarket's public trade feed.

The notable-trade and outcome models only generalize across traders if they
have seen many traders. The seeded watchlist is a curated handful; this module
sweeps the global ``/trades`` feed, ranks the wallets behind recent volume, and
(optionally) adds the new ones to the local watchlist so a bulk ingest can pull
their full history.

Network access reuses app.ingest's hardened httpx client (same SSL trust store,
timeouts, and retry/backoff), so discovery behaves like the rest of the app.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

import httpx
from sqlalchemy.orm import Session

from app.ingest import (
    DATA_API_BASE,
    _MAX_RETRY_ATTEMPTS,
    _INITIAL_BACKOFF_SECONDS,
    _polymarket_ssl_context,
    _polymarket_timeout,
)
from app.models import Trade, Wallet
from app.queries import normalize_tags, validate_wallet_address

logger = logging.getLogger(__name__)

# The global feed paginates like the per-user endpoint. Keep page size at the
# API's max and cap total pages so a discovery sweep stays bounded and polite.
_FEED_PAGE_SIZE = 500
_DEFAULT_PAGES = 10
DISCOVERY_TAG = "discovered"

# Polymarket's public leaderboard ranks real wallets by realized profit (PnL) or
# traded volume over a window. This is the highest-signal way to grow the universe
# with *proven* traders rather than whoever happened to trade in the last feed page.
LEADERBOARD_API_BASE = "https://lb-api.polymarket.com"
LEADERBOARD_KINDS = ("profit", "volume")
LEADERBOARD_WINDOWS = ("1d", "7d", "30d", "all")
_WINDOW_LABELS = {"1d": "24h", "7d": "7d", "30d": "30d", "all": "all-time"}
# The leaderboard tops out around the first few hundred ranks per window.
_LEADERBOARD_MAX_LIMIT = 1000


@dataclass(frozen=True)
class DiscoveredWallet:
    """A candidate wallet from the recent trade feed or a public leaderboard.

    ``volume``/``trade_count``/``market_count`` are populated by the feed sweep;
    ``profit`` and ``source`` describe leaderboard candidates (where per-wallet
    trade/market counts aren't available without a full ingest). ``note``, when
    set, overrides the default watchlist note so each source reads sensibly.
    """

    address: str
    name: str
    volume: float
    trade_count: int
    market_count: int
    profit: float = 0.0
    source: str = "feed"
    note: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "address": self.address,
            "name": self.name,
            "volume": self.volume,
            "trade_count": self.trade_count,
            "market_count": self.market_count,
            "profit": self.profit,
            "source": self.source,
        }


def _fetch_feed_page(limit: int, offset: int) -> List[Dict[str, Any]]:
    """Fetch one page of the global recent-trades feed (best-effort, retried)."""
    url = f"{DATA_API_BASE}/trades"
    params = {"limit": limit, "offset": offset}
    backoff = _INITIAL_BACKOFF_SECONDS
    for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=_polymarket_timeout(), verify=_polymarket_ssl_context()) as client:
                response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, list) else []
        except httpx.HTTPError:
            if attempt >= _MAX_RETRY_ATTEMPTS:
                logger.warning("Discovery feed page failed (offset=%s)", offset, exc_info=True)
                return []
            import time

            time.sleep(backoff)
            backoff *= 2
    return []


def discover_active_wallets(
    pages: int = _DEFAULT_PAGES,
    page_size: int = _FEED_PAGE_SIZE,
    exclude: Optional[Set[str]] = None,
) -> List[DiscoveredWallet]:
    """Sweep the recent-trades feed and rank wallets by recent USDC volume.

    Args:
        pages: Number of feed pages to sweep (each up to ``page_size`` trades).
        page_size: Trades per page (capped at the API maximum).
        exclude: Lowercased addresses to drop from the result (e.g. already
            tracked wallets).

    Returns:
        DiscoveredWallet records sorted by volume, highest first.
    """
    page_size = min(max(int(page_size), 1), _FEED_PAGE_SIZE)
    exclude = {a.lower() for a in (exclude or set())}

    agg: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"volume": 0.0, "trades": 0, "name": "", "markets": set()}
    )
    for page in range(pages):
        batch = _fetch_feed_page(limit=page_size, offset=page * page_size)
        if not batch:
            break
        for raw in batch:
            address = str(raw.get("proxyWallet") or "").strip().lower()
            if not address or address in exclude:
                continue
            try:
                price = float(raw.get("price") or 0)
                size = float(raw.get("size") or 0)
            except (TypeError, ValueError):
                continue
            entry = agg[address]
            entry["volume"] += price * size
            entry["trades"] += 1
            entry["name"] = entry["name"] or str(raw.get("name") or raw.get("pseudonym") or "")
            condition_id = str(raw.get("conditionId") or "").strip()
            if condition_id:
                entry["markets"].add(condition_id)
        if len(batch) < page_size:
            break  # exhausted the feed

    discovered = [
        DiscoveredWallet(
            address=address,
            name=data["name"] or "Anonymous",
            volume=round(data["volume"], 2),
            trade_count=data["trades"],
            market_count=len(data["markets"]),
        )
        for address, data in agg.items()
    ]
    discovered.sort(key=lambda w: w.volume, reverse=True)
    return discovered


def _fetch_leaderboard(kind: str, window: str, limit: int) -> List[Dict[str, Any]]:
    """Fetch one leaderboard page (best-effort, retried like the feed sweep)."""
    url = f"{LEADERBOARD_API_BASE}/{kind}"
    params = {"window": window, "limit": limit}
    backoff = _INITIAL_BACKOFF_SECONDS
    for attempt in range(1, _MAX_RETRY_ATTEMPTS + 1):
        try:
            with httpx.Client(timeout=_polymarket_timeout(), verify=_polymarket_ssl_context()) as client:
                response = client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, list) else []
        except httpx.HTTPError:
            if attempt >= _MAX_RETRY_ATTEMPTS:
                logger.warning("Leaderboard fetch failed (%s/%s)", kind, window, exc_info=True)
                return []
            import time

            time.sleep(backoff)
            backoff *= 2
    return []


def discover_leaderboard_wallets(
    kind: str = "profit",
    window: str = "all",
    limit: int = 200,
    exclude: Optional[Set[str]] = None,
) -> List[DiscoveredWallet]:
    """Pull proven traders from Polymarket's public profit/volume leaderboard.

    Args:
        kind: ``"profit"`` (realized PnL) or ``"volume"`` (USDC traded).
        window: ``"1d"``, ``"7d"``, ``"30d"`` or ``"all"`` (all-time).
        limit: Max ranks to request (capped at the API ceiling).
        exclude: Lowercased addresses to drop (e.g. already-tracked wallets).

    Returns:
        DiscoveredWallet records in leaderboard rank order (best first). For the
        profit board ``profit`` carries realized PnL; for volume ``volume`` does.
    """
    if kind not in LEADERBOARD_KINDS:
        kind = "profit"
    if window not in LEADERBOARD_WINDOWS:
        window = "all"
    limit = min(max(int(limit), 1), _LEADERBOARD_MAX_LIMIT)
    exclude = {a.lower() for a in (exclude or set())}
    window_label = _WINDOW_LABELS[window]

    rows = _fetch_leaderboard(kind, window, limit)
    discovered: List[DiscoveredWallet] = []
    seen: Set[str] = set()
    for raw in rows:
        address = str(raw.get("proxyWallet") or "").strip().lower()
        if not address or address in exclude or address in seen:
            continue
        try:
            amount = float(raw.get("amount") or 0)
        except (TypeError, ValueError):
            amount = 0.0
        name = str(raw.get("name") or raw.get("pseudonym") or "") or "Anonymous"
        seen.add(address)
        if kind == "profit":
            note = f"Top-profit leaderboard wallet: ~${amount:,.0f} realized profit ({window_label})."
            profit, volume = round(amount, 2), 0.0
        else:
            note = f"Top-volume leaderboard wallet: ~${amount:,.0f} traded ({window_label})."
            profit, volume = 0.0, round(amount, 2)
        discovered.append(
            DiscoveredWallet(
                address=address,
                name=name,
                volume=volume,
                trade_count=0,
                market_count=0,
                profit=profit,
                source=f"leaderboard:{kind}",
                note=note,
            )
        )
    return discovered


def tracked_addresses(db: Session) -> Set[str]:
    """Lowercased set of every wallet address already in the watchlist."""
    return {row[0].lower() for row in db.query(Wallet.address).all()}


def add_discovered_wallets(
    db: Session,
    discovered: Iterable[DiscoveredWallet],
    *,
    min_volume: float = 0.0,
    min_trades: int = 1,
    max_add: Optional[int] = None,
    tag: str = DISCOVERY_TAG,
) -> Dict[str, Any]:
    """Insert new, valid, sufficiently-active discovered wallets into the watchlist.

    Idempotent: existing wallets are skipped (never modified). Returns a summary
    with the addresses actually added.
    """
    existing = tracked_addresses(db)
    normalized_tag = normalize_tags(tag) or None
    added: List[str] = []
    skipped_existing = 0
    skipped_threshold = 0
    skipped_invalid = 0

    for wallet in discovered:
        if max_add is not None and len(added) >= max_add:
            break
        address = wallet.address.strip().lower()
        if validate_wallet_address(address) is not None:
            skipped_invalid += 1
            continue
        if address in existing:
            skipped_existing += 1
            continue
        if wallet.volume < min_volume or wallet.trade_count < min_trades:
            skipped_threshold += 1
            continue
        label = wallet.name if wallet.name and wallet.name != "Anonymous" else None
        suffix = {
            "leaderboard:profit": "top profit",
            "leaderboard:volume": "top volume",
        }.get(wallet.source, "discovered")
        note = wallet.note or (
            f"Auto-discovered from recent feed: ~${wallet.volume:,.0f} volume, "
            f"{wallet.trade_count} trades across {wallet.market_count} markets."
        )
        db.add(
            Wallet(
                address=address,
                label=f"{label} ({suffix})" if label else f"{suffix.capitalize()} wallet",
                tags=normalized_tag,
                notes=note,
            )
        )
        existing.add(address)
        added.append(address)

    db.flush()
    return {
        "added": added,
        "added_count": len(added),
        "skipped_existing": skipped_existing,
        "skipped_threshold": skipped_threshold,
        "skipped_invalid": skipped_invalid,
        "candidates": sum(1 for _ in discovered) if isinstance(discovered, (list, tuple)) else None,
    }
