# Summary: Tiny in-process TTL cache for expensive read-mostly aggregations.
# Details: Dashboard/leaderboard/trades pages recompute full-table aggregations
# (hundreds of thousands of trade rows) on every request. The underlying data
# only changes on a manual wallet refresh, so caching the plain-data results for
# a short window makes tab-to-tab navigation near-instant while staying fresh.
# The cache is also explicitly invalidated whenever new trades are ingested.
#
# Only cache plain data (dicts/lists/scalars). Never cache SQLAlchemy ORM
# instances - they are bound to a session that closes at the end of the request.
import threading
import time
from typing import Any, Callable, Dict, Tuple

# Bounded staleness for anything not covered by explicit invalidation. Trade
# data changes only on manual refresh (which calls invalidate()), so this is a
# safety net rather than the primary freshness mechanism.
DEFAULT_TTL_SECONDS = 60.0

_lock = threading.Lock()
_store: Dict[str, Tuple[float, Any]] = {}


def cached(key: str, compute: Callable[[], Any], ttl: float = DEFAULT_TTL_SECONDS) -> Any:
    """Return the cached value for ``key`` or compute, store, and return it.

    ``compute`` runs outside the lock so a slow aggregation never blocks other
    cache readers. A brief duplicate computation under concurrency is harmless.
    """
    now = time.monotonic()
    with _lock:
        hit = _store.get(key)
        if hit is not None and hit[0] > now:
            return hit[1]
    value = compute()
    with _lock:
        _store[key] = (now + ttl, value)
    return value


def invalidate() -> None:
    """Drop all cached entries. Call after any change to trade data."""
    with _lock:
        _store.clear()
