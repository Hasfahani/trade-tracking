#!/usr/bin/env python
# Summary: Rebuilds saved retention stats.
# Details: It is a command-line helper for setup, maintenance, migration, backup, scoring, or operational checks.
"""Backfill retention_daily and retention_weekly from event_log.

Computes and upserts pre-aggregated retention snapshots for the last N days.
Safe to re-run â€” rows are replaced if they already exist.

Usage:
    python scripts/backfill_retention.py [--days 30]

Requires the app to be importable (run from the repo root):
    cd "c:\\trade tracking"
    python scripts/backfill_retention.py --days 30
"""
import argparse
import logging
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

# Ensure repo root is on sys.path when run as a script
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_retention")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _isodate(d: date) -> str:
    return d.isoformat()


def backfill(days: int = 30) -> None:
    from sqlalchemy import text

    from app.db import SessionLocal
    from app.models import RetentionDaily, RetentionWeekly

    db = SessionLocal()
    try:
        _backfill_daily(db, days)
        _backfill_weekly(db, days)
        db.commit()
        logger.info("Backfill complete.")
    except Exception:
        logger.exception("Backfill failed â€” rolling back")
        db.rollback()
        raise
    finally:
        db.close()


def _backfill_daily(db, days: int) -> None:
    from sqlalchemy import text

    today = _utcnow().date()
    since = today - timedelta(days=days)
    logger.info("Backfilling retention_daily from %s to %s", since, today)

    # Pull all (tracker_id, date, event_name) in window
    rows = db.execute(
        text(
            "SELECT tracker_id, DATE(event_ts) AS event_date, event_name"
            " FROM event_log WHERE event_ts >= :s ORDER BY event_date"
        ),
        {"s": since.isoformat()},
    ).fetchall()

    # Build daily active-user sets and event counts
    daily_users: dict = defaultdict(set)
    daily_impressions: dict = defaultdict(set)
    daily_opens: dict = defaultdict(set)

    for tracker_id, event_date_raw, event_name in rows:
        d = (
            datetime.strptime(event_date_raw, "%Y-%m-%d").date()
            if isinstance(event_date_raw, str)
            else event_date_raw
        )
        daily_users[d].add(tracker_id)
        if event_name == "alert_impression":
            daily_impressions[d].add(tracker_id)
        elif event_name == "alert_open":
            daily_opens[d].add(tracker_id)

    inserted = 0
    day = since
    while day <= today:
        users_today = daily_users.get(day, set())
        dau = len(users_today)

        # WAU rolling 7: count distinct users in [day-6, day]
        wau_window: set = set()
        for offset in range(7):
            wau_window |= daily_users.get(day - timedelta(days=offset), set())
        wau7 = len(wau_window)

        # Alert open rate for this day
        imp = len(daily_impressions.get(day, set()))
        opens = len(daily_opens.get(day, set()))
        aor = round((opens / imp) * 100, 1) if imp else None

        # D1: % of today's users also active tomorrow
        tomorrow = day + timedelta(days=1)
        users_tomorrow = daily_users.get(tomorrow, set())
        d1 = (
            round(len(users_today & users_tomorrow) / len(users_today) * 100, 1)
            if users_today and users_tomorrow
            else None
        )

        # D7: % of today's users active in [day+7, day+13]
        future: set = set()
        for offset in range(7, 14):
            future |= daily_users.get(day + timedelta(days=offset), set())
        d7 = (
            round(len(users_today & future) / len(users_today) * 100, 1)
            if users_today and future
            else None
        )

        # Sessions per user for trailing 7 days
        week_days = [day - timedelta(days=o) for o in range(7)]
        week_user_days: dict = defaultdict(int)
        for wd in week_days:
            for uid in daily_users.get(wd, set()):
                week_user_days[uid] += 1
        spu = (
            round(sum(week_user_days.values()) / len(week_user_days), 2)
            if week_user_days
            else None
        )

        # Upsert
        from app.models import RetentionDaily

        existing = db.get(RetentionDaily, day)
        if existing:
            existing.dau = dau
            existing.wau_rolling_7 = wau7
            existing.alert_impressions_users = imp
            existing.alert_open_users = opens
            existing.alert_open_rate = aor
            existing.d1_return_rate = d1
            existing.d7_return_rate = d7
            existing.sessions_per_user = spu
            existing.computed_at = _utcnow()
        else:
            db.add(
                RetentionDaily(
                    date=day,
                    dau=dau,
                    wau_rolling_7=wau7,
                    alert_impressions_users=imp,
                    alert_open_users=opens,
                    alert_open_rate=aor,
                    d1_return_rate=d1,
                    d7_return_rate=d7,
                    sessions_per_user=spu,
                    computed_at=_utcnow(),
                )
            )
        inserted += 1
        day += timedelta(days=1)

    logger.info("retention_daily: upserted %d rows", inserted)


def _backfill_weekly(db, days: int) -> None:
    from sqlalchemy import text

    today = _utcnow().date()
    # Start from the ISO Monday on or before (today - days)
    since_raw = today - timedelta(days=days)
    week_start = since_raw - timedelta(days=since_raw.weekday())

    logger.info("Backfilling retention_weekly from week %s", week_start)

    rows = db.execute(
        text(
            "SELECT tracker_id, DATE(event_ts) AS event_date, event_name"
            " FROM event_log WHERE event_ts >= :s ORDER BY event_date"
        ),
        {"s": week_start.isoformat()},
    ).fetchall()

    daily_users: dict = defaultdict(set)
    daily_impressions: dict = defaultdict(set)
    daily_opens: dict = defaultdict(set)

    for tracker_id, event_date_raw, event_name in rows:
        d = (
            datetime.strptime(event_date_raw, "%Y-%m-%d").date()
            if isinstance(event_date_raw, str)
            else event_date_raw
        )
        daily_users[d].add(tracker_id)
        if event_name == "alert_impression":
            daily_impressions[d].add(tracker_id)
        elif event_name == "alert_open":
            daily_opens[d].add(tracker_id)

    upserted = 0
    ws = week_start
    while ws <= today:
        week_days = [ws + timedelta(days=o) for o in range(7)]
        week_users: set = set()
        week_imp: set = set()
        week_opens: set = set()
        user_day_count: dict = defaultdict(int)

        for wd in week_days:
            for uid in daily_users.get(wd, set()):
                week_users.add(uid)
                user_day_count[uid] += 1
            week_imp |= daily_impressions.get(wd, set())
            week_opens |= daily_opens.get(wd, set())

        wau = len(week_users)
        imp = len(week_imp)
        opens_count = len(week_opens)
        aor = round((opens_count / imp) * 100, 1) if imp else None
        spu = round(sum(user_day_count.values()) / len(user_day_count), 2) if user_day_count else None

        # Repeat users: active in this week AND prior week
        prior_week_users: set = set()
        for wd in [ws - timedelta(days=o + 1) for o in range(7)]:
            prior_week_users |= daily_users.get(wd, set())
        repeat = len(week_users & prior_week_users)

        from app.models import RetentionWeekly

        existing = db.get(RetentionWeekly, ws)
        if existing:
            existing.wau = wau
            existing.repeat_users = repeat
            existing.sessions_per_user = spu
            existing.alert_open_rate = aor
            existing.computed_at = _utcnow()
        else:
            db.add(
                RetentionWeekly(
                    week_start=ws,
                    wau=wau,
                    repeat_users=repeat,
                    sessions_per_user=spu,
                    alert_open_rate=aor,
                    computed_at=_utcnow(),
                )
            )
        upserted += 1
        ws += timedelta(days=7)

    logger.info("retention_weekly: upserted %d rows", upserted)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill retention metrics from event_log")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days to backfill (default: 30)",
    )
    args = parser.parse_args()
    backfill(days=args.days)
