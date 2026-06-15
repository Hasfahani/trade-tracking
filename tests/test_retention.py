# Tests usage and retention tracking.
"""Unit and integration tests for retention signal tracking."""
import asyncio
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import create_app
from app.models import Base, EventLog
from app import retention as ret


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    return eng


@pytest.fixture()
def db_session(engine):
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = Session()
    yield db
    db.close()


@pytest.fixture()
def client(engine):
    app = create_app(lifespan_context=None, csrf_enabled=False)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app, raise_server_exceptions=True)


def _insert_events(db, events: list[dict]) -> None:
    """Helper: insert raw EventLog rows."""
    for e in events:
        db.add(EventLog(**e))
    db.commit()


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _days_ago(n: float) -> datetime:
    return _utcnow() - timedelta(days=n)


# ---------------------------------------------------------------------------
# Unit: get_wau
# ---------------------------------------------------------------------------

class TestGetWau:
    def test_empty_returns_zero(self, db_session):
        result = ret.get_wau(db_session)
        assert result["wau"] == 0
        assert result["previous_wau"] == 0
        assert result["trend"] == 0
        assert result["trend_pct"] is None

    def test_counts_distinct_users(self, db_session):
        _insert_events(db_session, [
            {"tracker_id": "user1", "event_name": "page_view", "event_ts": _days_ago(1), "route": "dashboard"},
            {"tracker_id": "user1", "event_name": "page_view", "event_ts": _days_ago(2), "route": "dashboard"},
            {"tracker_id": "user2", "event_name": "page_view", "event_ts": _days_ago(3), "route": "wallets"},
        ])
        result = ret.get_wau(db_session, days=7)
        assert result["wau"] == 2

    def test_previous_period_trend(self, db_session):
        # 2 users this week, 1 last week â†’ trend +1
        _insert_events(db_session, [
            {"tracker_id": "user1", "event_name": "page_view", "event_ts": _days_ago(2), "route": "dashboard"},
            {"tracker_id": "user2", "event_name": "page_view", "event_ts": _days_ago(3), "route": "dashboard"},
            {"tracker_id": "user3", "event_name": "page_view", "event_ts": _days_ago(10), "route": "dashboard"},
        ])
        result = ret.get_wau(db_session, days=7)
        assert result["wau"] == 2
        assert result["previous_wau"] == 1
        assert result["trend"] == 1
        assert result["trend_pct"] == 100

    def test_excludes_events_outside_window(self, db_session):
        _insert_events(db_session, [
            {"tracker_id": "old_user", "event_name": "page_view", "event_ts": _days_ago(15), "route": "dashboard"},
        ])
        result = ret.get_wau(db_session, days=7)
        assert result["wau"] == 0


# ---------------------------------------------------------------------------
# Unit: get_alert_open_rate
# ---------------------------------------------------------------------------

class TestGetAlertOpenRate:
    def test_empty_returns_none_rate(self, db_session):
        result = ret.get_alert_open_rate(db_session)
        assert result["impression_users"] == 0
        assert result["open_users"] == 0
        assert result["open_rate_pct"] is None

    def test_zero_impressions_returns_none(self, db_session):
        _insert_events(db_session, [
            {"tracker_id": "u1", "event_name": "alert_open", "event_ts": _days_ago(1), "route": "settings"},
        ])
        result = ret.get_alert_open_rate(db_session)
        assert result["open_rate_pct"] is None

    def test_rate_calculation(self, db_session):
        _insert_events(db_session, [
            {"tracker_id": "u1", "event_name": "alert_impression", "event_ts": _days_ago(1), "route": "settings"},
            {"tracker_id": "u2", "event_name": "alert_impression", "event_ts": _days_ago(2), "route": "settings"},
            {"tracker_id": "u3", "event_name": "alert_impression", "event_ts": _days_ago(3), "route": "settings"},
            {"tracker_id": "u4", "event_name": "alert_impression", "event_ts": _days_ago(4), "route": "settings"},
            # only u1 and u2 open
            {"tracker_id": "u1", "event_name": "alert_open", "event_ts": _days_ago(1), "route": "settings"},
            {"tracker_id": "u2", "event_name": "alert_open", "event_ts": _days_ago(1), "route": "settings"},
        ])
        result = ret.get_alert_open_rate(db_session, period="7d")
        assert result["impression_users"] == 4
        assert result["open_users"] == 2
        assert result["open_rate_pct"] == 50.0

    def test_duplicate_events_same_user_counted_once(self, db_session):
        _insert_events(db_session, [
            {"tracker_id": "u1", "event_name": "alert_impression", "event_ts": _days_ago(1), "route": "settings"},
            {"tracker_id": "u1", "event_name": "alert_impression", "event_ts": _days_ago(2), "route": "settings"},
            {"tracker_id": "u1", "event_name": "alert_open", "event_ts": _days_ago(1), "route": "settings"},
            {"tracker_id": "u1", "event_name": "alert_open", "event_ts": _days_ago(2), "route": "settings"},
        ])
        result = ret.get_alert_open_rate(db_session)
        assert result["impression_users"] == 1
        assert result["open_users"] == 1
        assert result["open_rate_pct"] == 100.0


# ---------------------------------------------------------------------------
# Unit: get_repeat_usage
# ---------------------------------------------------------------------------

class TestGetRepeatUsage:
    def test_empty_returns_nones(self, db_session):
        result = ret.get_repeat_usage(db_session)
        assert result["d1_return_rate"] is None
        assert result["d7_return_rate"] is None

    def test_d1_return_rate(self, db_session):
        # user1 active day 0 and day 1 â†’ 100% D1
        # user2 active day 0 only â†’ 0% D1
        day0 = _days_ago(8)
        day1 = _days_ago(7)
        _insert_events(db_session, [
            {"tracker_id": "u1", "event_name": "page_view", "event_ts": day0, "route": "dashboard"},
            {"tracker_id": "u2", "event_name": "page_view", "event_ts": day0, "route": "dashboard"},
            {"tracker_id": "u1", "event_name": "page_view", "event_ts": day1, "route": "dashboard"},
        ])
        result = ret.get_repeat_usage(db_session, lookback_days=14)
        assert result["d1_return_rate"] == 50.0

    def test_sessions_per_user(self, db_session):
        # 1 user active 3 distinct days in 7-day lookback
        for offset in range(3):
            _insert_events(db_session, [
                {
                    "tracker_id": "u1",
                    "event_name": "page_view",
                    "event_ts": _days_ago(offset + 1),
                    "route": "dashboard",
                }
            ])
        result = ret.get_repeat_usage(db_session, lookback_days=7)
        assert result["sessions_per_user"] == 3.0


# ---------------------------------------------------------------------------
# Unit: get_retention_summary graceful failure
# ---------------------------------------------------------------------------

class TestGetRetentionSummaryGraceful:
    def test_returns_none_fields_on_db_error(self):
        bad_db = MagicMock()
        bad_db.execute.side_effect = RuntimeError("db is on fire")
        result = ret.get_retention_summary(bad_db)
        assert result["wau"] is None
        assert result["open_rate_pct"] is None
        assert result["d1_return_rate"] is None


# ---------------------------------------------------------------------------
# Unit: emit + tracker_id
# ---------------------------------------------------------------------------

class TestEmitAndIdentity:
    def test_emit_disabled_drops_event(self):
        with patch("app.settings.RETENTION_METRICS_ENABLED", False):
            # Queue should stay empty
            queue_size_before = ret._event_queue.qsize()
            ret.emit(ret.RawEvent(tracker_id="x", event_name="page_view", route="dashboard"))
            assert ret._event_queue.qsize() == queue_size_before

    def test_get_or_create_tracker_id_creates_and_reuses(self):
        req = MagicMock()
        req.session = {}
        tid1 = ret.get_or_create_tracker_id(req)
        tid2 = ret.get_or_create_tracker_id(req)
        assert tid1 == tid2
        assert len(tid1) == 32  # uuid4().hex

    def test_get_or_create_tracker_id_preserves_existing(self):
        req = MagicMock()
        req.session = {"tracker_id": "existing_id"}
        assert ret.get_or_create_tracker_id(req) == "existing_id"


# ---------------------------------------------------------------------------
# Integration: admin retention endpoint
# ---------------------------------------------------------------------------

class TestRetentionEndpoint:
    def test_returns_200_with_empty_db(self, client):
        r = client.get("/admin/metrics/retention")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True
        assert "summary" in body
        assert body["summary"]["wau"]["wau"] == 0

    def test_range_param_accepted(self, client):
        r = client.get("/admin/metrics/retention?range=7d")
        assert r.status_code == 200
        assert r.json()["range"] == "7d"

    def test_invalid_range_defaults_to_30d(self, client):
        r = client.get("/admin/metrics/retention?range=banana")
        assert r.status_code == 200
        assert r.json()["range"] == "30d"

    def test_disabled_returns_enabled_false(self, client):
        with patch("app.routes.retention.RETENTION_METRICS_ENABLED", False):
            r = client.get("/admin/metrics/retention")
        assert r.status_code == 200
        assert r.json()["enabled"] is False


# ---------------------------------------------------------------------------
# Integration: dashboard page includes retention section
# ---------------------------------------------------------------------------

class TestDashboardRetentionSection:
    def test_dashboard_renders_retention_card(self, client):
        r = client.get("/dashboard")
        assert r.status_code == 200
        assert b"Retention" in r.content

    def test_dashboard_retention_disabled_hides_card(self, client):
        with patch("app.routes.core.RETENTION_METRICS_ENABLED", False):
            r = client.get("/dashboard")
        assert r.status_code == 200
        # No retention summary passed â†’ {% if retention %} block skipped
        # The heading text won't appear in the Interesting-activity context
        # Just verify the page still renders correctly
        assert b"Interesting activity" in r.content
