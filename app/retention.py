# Tracks app usage and retention stats.
"""Retention signal tracking: event ingestion, identity, and live metrics queries.

Event flow:
  route handler -> emit() -> asyncio.Queue -> _drain_loop() -> event_log table

Identity:
  A stable ``tracker_id`` (random hex UUID) is stored in the Starlette session
  cookie on first visit. No PII is stored beyond this opaque identifier.

Feature flag:
  Set RETENTION_METRICS_ENABLED=false to silence all event writes.
"""
import asyncio
import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_event_queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
_drain_task: Optional[asyncio.Task] = None

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def get_or_create_tracker_id(request) -> str:
    """Return a stable session-scoped tracker ID, generating one if absent."""
    if "tracker_id" not in request.session:
        request.session["tracker_id"] = uuid.uuid4().hex
    return request.session["tracker_id"]


# ---------------------------------------------------------------------------
# Event emission (non-blocking)
# ---------------------------------------------------------------------------

@dataclass
class RawEvent:
    tracker_id: str
    event_name: str
    route: str
    event_ts: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    metadata: Dict[str, Any] = field(default_factory=dict)
    alert_id: Optional[str] = None


def emit(event: RawEvent) -> None:
    """Enqueue an event for async DB write. Never raises; silently drops if queue full."""
    from app.settings import RETENTION_METRICS_ENABLED

    if not RETENTION_METRICS_ENABLED:
        return
    try:
        _event_queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning("retention: event queue full, dropping %s", event.event_name)
    except Exception:
        logger.exception("retention: failed to enqueue event")


# ---------------------------------------------------------------------------
# Background drain task
# ---------------------------------------------------------------------------

async def _drain_loop() -> None:
    """Drain the event queue in batches and persist to event_log."""
    from app.db import SessionLocal
    from app.models import EventLog

    while True:
        batch: List[RawEvent] = []
        try:
            ev = await asyncio.wait_for(_event_queue.get(), timeout=5.0)
            batch.append(ev)
            while not _event_queue.empty() and len(batch) < 200:
                batch.append(_event_queue.get_nowait())
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("retention: drain loop error")
            continue

        db = None
        try:
            db = SessionLocal()
            db.bulk_save_objects(
                [
                    EventLog(
                        tracker_id=e.tracker_id,
                        event_name=e.event_name,
                        event_ts=e.event_ts,
                        route=e.route,
                        metadata_json=json.dumps(e.metadata) if e.metadata else None,
                        alert_id=e.alert_id,
                    )
                    for e in batch
                ]
            )
            db.commit()
        except Exception:
            logger.exception("retention: failed to persist %d events", len(batch))
        finally:
            if db is not None:
                db.close()


async def start_drain() -> None:
    """Start the background event drain task. Call from lifespan startup."""
    global _drain_task
    if _drain_task and not _drain_task.done():
        return
    _drain_task = asyncio.create_task(_drain_loop(), name="retention-drain")
    logger.info("retention: drain task started")


async def stop_drain() -> None:
    """Cancel the drain task. Call from lifespan shutdown."""
    global _drain_task
    if _drain_task and not _drain_task.done():
        _drain_task.cancel()
        try:
            await _drain_task
        except asyncio.CancelledError:
            pass
    logger.info("retention: drain task stopped")


# ---------------------------------------------------------------------------
# Live metric queries (computed directly from event_log)
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_wau(db, days: int = 7) -> Dict:
    """Rolling WAU for the last ``days`` days, with previous-period trend."""
    from sqlalchemy import text

    now = _utcnow()
    cur_start = now - timedelta(days=days)
    prev_start = now - timedelta(days=days * 2)

    current = (
        db.execute(
            text(
                "SELECT COUNT(DISTINCT tracker_id) FROM event_log"
                " WHERE event_ts >= :s"
            ),
            {"s": cur_start},
        ).scalar()
        or 0
    )
    previous = (
        db.execute(
            text(
                "SELECT COUNT(DISTINCT tracker_id) FROM event_log"
                " WHERE event_ts >= :s AND event_ts < :e"
            ),
            {"s": prev_start, "e": cur_start},
        ).scalar()
        or 0
    )
    trend = current - previous
    trend_pct = round((trend / previous) * 100) if previous else None
    return {
        "wau": current,
        "previous_wau": previous,
        "trend": trend,
        "trend_pct": trend_pct,
    }


def get_alert_open_rate(db, period: str = "7d") -> Dict:
    """Alert open rate (unique users who opened vs. were shown an alert)."""
    from sqlalchemy import text

    days = {"7d": 7, "30d": 30}.get(period, 7)
    since = _utcnow() - timedelta(days=days)

    imp = (
        db.execute(
            text(
                "SELECT COUNT(DISTINCT tracker_id) FROM event_log"
                " WHERE event_name = 'alert_impression' AND event_ts >= :s"
            ),
            {"s": since},
        ).scalar()
        or 0
    )
    opens = (
        db.execute(
            text(
                "SELECT COUNT(DISTINCT tracker_id) FROM event_log"
                " WHERE event_name = 'alert_open' AND event_ts >= :s"
            ),
            {"s": since},
        ).scalar()
        or 0
    )
    rate = round((opens / imp) * 100, 1) if imp > 0 else None
    return {
        "period": period,
        "impression_users": imp,
        "open_users": opens,
        "open_rate_pct": rate,
    }


def get_repeat_usage(db, lookback_days: int = 30) -> Dict:
    """D1/D7 return rates and sessions-per-user over the lookback window.

    D1: average % of daily active users who returned the next day.
    D7: average % of daily active users who returned within the following 7-day window.
    sessions_per_user: average active days per user per calendar week.
    """
    from sqlalchemy import text

    since = _utcnow() - timedelta(days=lookback_days)
    rows = db.execute(
        text(
            "SELECT tracker_id, DATE(event_ts) AS event_date"
            " FROM event_log WHERE event_ts >= :s"
            " GROUP BY tracker_id, DATE(event_ts)"
        ),
        {"s": since},
    ).fetchall()

    if not rows:
        return {
            "d1_return_rate": None,
            "d7_return_rate": None,
            "sessions_per_user": None,
        }

    # Per-user date sets
    user_dates: Dict[str, set] = defaultdict(set)
    for tracker_id, event_date in rows:
        if isinstance(event_date, str):
            event_date = datetime.strptime(event_date, "%Y-%m-%d").date()
        user_dates[tracker_id].add(event_date)

    # Daily active user sets
    daily: Dict[date, set] = defaultdict(set)
    for tracker_id, dates in user_dates.items():
        for d in dates:
            daily[d].add(tracker_id)

    # D1
    d1_rates: List[float] = []
    for day, users in daily.items():
        next_day = day + timedelta(days=1)
        if next_day in daily and users:
            d1_rates.append(len(users & daily[next_day]) / len(users))

    # D7: % who appear in [day+7, day+13]
    d7_rates: List[float] = []
    for day, users in daily.items():
        future: set = set()
        for offset in range(7, 14):
            future |= daily.get(day + timedelta(days=offset), set())
        if users and future:
            d7_rates.append(len(users & future) / len(users))

    total_days = sum(len(dates) for dates in user_dates.values())
    unique_users = len(user_dates)
    weeks = max(1, lookback_days / 7)
    spu = round(total_days / unique_users / weeks, 2) if unique_users else None

    return {
        "d1_return_rate": round(sum(d1_rates) / len(d1_rates) * 100, 1) if d1_rates else None,
        "d7_return_rate": round(sum(d7_rates) / len(d7_rates) * 100, 1) if d7_rates else None,
        "sessions_per_user": spu,
    }


def get_retention_summary(db) -> Dict:
    """Aggregate all retention signals into a single dict for the dashboard."""
    try:
        wau = get_wau(db)
    except Exception:
        logger.exception("retention: get_wau failed")
        wau = {"wau": None, "previous_wau": None, "trend": None, "trend_pct": None}

    try:
        aor = get_alert_open_rate(db)
    except Exception:
        logger.exception("retention: get_alert_open_rate failed")
        aor = {"impression_users": None, "open_users": None, "open_rate_pct": None}

    try:
        repeat = get_repeat_usage(db)
    except Exception:
        logger.exception("retention: get_repeat_usage failed")
        repeat = {"d1_return_rate": None, "d7_return_rate": None, "sessions_per_user": None}

    return {**wau, **aor, **repeat}


def get_daily_series(db, days: int = 30) -> List[Dict]:
    """Return pre-computed daily series from retention_daily (if populated)."""
    from sqlalchemy import text

    since = (_utcnow() - timedelta(days=days)).date().isoformat()
    rows = db.execute(
        text(
            "SELECT date, dau, wau_rolling_7, alert_open_rate,"
            " d1_return_rate, d7_return_rate"
            " FROM retention_daily WHERE date >= :s ORDER BY date"
        ),
        {"s": since},
    ).fetchall()
    return [
        {
            "date": str(r[0]),
            "dau": r[1],
            "wau_rolling_7": r[2],
            "alert_open_rate": r[3],
            "d1_return_rate": r[4],
            "d7_return_rate": r[5],
        }
        for r in rows
    ]


def get_weekly_series(db, weeks: int = 8) -> List[Dict]:
    """Return pre-computed weekly series from retention_weekly (if populated)."""
    from sqlalchemy import text

    since = (_utcnow() - timedelta(days=weeks * 7)).date().isoformat()
    rows = db.execute(
        text(
            "SELECT week_start, wau, repeat_users, sessions_per_user, alert_open_rate"
            " FROM retention_weekly WHERE week_start >= :s ORDER BY week_start"
        ),
        {"s": since},
    ).fetchall()
    return [
        {
            "week_start": str(r[0]),
            "wau": r[1],
            "repeat_users": r[2],
            "sessions_per_user": r[3],
            "alert_open_rate": r[4],
        }
        for r in rows
    ]
