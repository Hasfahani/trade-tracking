# Summary: Tests dashboard stats.
# Details: It checks this part of the project so future code changes do not silently break expected behavior.
"""Tests for app/analytics.py functions with known inputs."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import analytics
from app.models import Base, SyncEvent, Trade, Wallet


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


_ADDR = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_ADDR2 = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _wallet(db, address=_ADDR, label=None):
    w = Wallet(address=address, label=label)
    db.add(w)
    return w


def _trade(db, address=_ADDR, trade_id="t1", condition_id="cond-1", *, side="YES", price=0.5, size=10.0, hours_ago=0):
    now = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago)
    t = Trade(
        wallet_address=address,
        trade_id=trade_id,
        condition_id=condition_id,
        market_title=f"Market {trade_id}",
        side=side,
        price=price,
        size=size,
        traded_at=now,
    )
    db.add(t)
    return t


class TestDetectInterestingActivity:
    def test_empty_returns_empty_list(self, db):
        assert analytics.detect_interesting_activity(db) == []

    def test_detects_large_trade(self, db):
        _wallet(db, label="Big Player")
        _trade(db, price=0.8, size=500.0)  # value = 400 > threshold
        db.commit()
        events = analytics.detect_interesting_activity(db)
        large = [e for e in events if e["type"] == "large_trade"]
        assert len(large) == 1
        assert large[0]["value"] == pytest.approx(400.0)
        assert large[0]["label"] == "Big Player"

    def test_skips_small_trade(self, db):
        _wallet(db)
        _trade(db, price=0.1, size=1.0)  # value = 0.1 < threshold
        db.commit()
        events = analytics.detect_interesting_activity(db)
        assert not any(e["type"] == "large_trade" for e in events)

    def test_detects_activity_spike(self, db):
        _wallet(db)
        for i in range(4):
            _trade(db, trade_id=f"spike-{i}", condition_id=f"cond-{i}", hours_ago=i / 60)
        db.commit()
        events = analytics.detect_interesting_activity(db)
        spikes = [e for e in events if e["type"] == "activity_spike"]
        assert len(spikes) == 1
        assert spikes[0]["count"] >= 3

    def test_detects_new_market_entry(self, db):
        _wallet(db)
        _trade(db, condition_id="brand-new-market")
        db.commit()
        events = analytics.detect_interesting_activity(db)
        new_markets = [e for e in events if e["type"] == "new_market"]
        assert len(new_markets) >= 1
        assert any(e["wallet"] == _ADDR for e in new_markets)

    def test_old_market_not_flagged_as_new(self, db):
        _wallet(db)
        _trade(db, trade_id="old-entry", condition_id="old-cond", hours_ago=48)
        _trade(db, trade_id="recent-follow", condition_id="old-cond", hours_ago=1)
        db.commit()
        events = analytics.detect_interesting_activity(db)
        new_markets = [e for e in events if e["type"] == "new_market"]
        assert not any(e.get("market") and "old-cond" in str(e.get("market", "")) for e in new_markets)

    def test_capped_at_ten_results(self, db):
        _wallet(db)
        for i in range(20):
            _trade(db, trade_id=f"big-{i}", condition_id=f"cond-{i}", price=0.8, size=500.0)
        db.commit()
        events = analytics.detect_interesting_activity(db)
        assert len(events) <= 10

    def test_uses_label_fallback_for_unlabeled_wallet(self, db):
        _wallet(db, label=None)
        _trade(db, price=0.8, size=500.0)
        db.commit()
        events = analytics.detect_interesting_activity(db)
        large = [e for e in events if e["type"] == "large_trade"]
        assert large[0]["label"] != _ADDR
        assert "..." in large[0]["label"]


class TestGetWalletIntelligenceSummary:
    def test_inactive_wallet_returns_correct_level(self, db):
        _wallet(db)
        db.commit()
        result = analytics.get_wallet_intelligence_summary(db, _ADDR)
        assert result["activity_level"] == "Inactive"
        assert result["trades_last_24h"] == 0
        assert result["total_trades"] == 0

    def test_single_recent_trade_gives_low_level(self, db):
        _wallet(db)
        _trade(db)
        db.commit()
        result = analytics.get_wallet_intelligence_summary(db, _ADDR)
        assert result["activity_level"] == "Low"
        assert result["trades_last_24h"] == 1

    def test_ten_or_more_recent_trades_gives_high_level(self, db):
        _wallet(db)
        for i in range(12):
            _trade(db, trade_id=f"high-{i}", condition_id=f"c-{i}")
        db.commit()
        result = analytics.get_wallet_intelligence_summary(db, _ADDR)
        assert result["activity_level"] == "High"

    def test_old_trades_not_counted_in_24h_metrics(self, db):
        _wallet(db)
        _trade(db, trade_id="recent", hours_ago=1)
        _trade(db, trade_id="old", condition_id="cond-old", hours_ago=48)
        db.commit()
        result = analytics.get_wallet_intelligence_summary(db, _ADDR)
        assert result["trades_last_24h"] == 1
        assert result["total_trades"] == 2

    def test_average_trade_size_calculation(self, db):
        _wallet(db)
        _trade(db, trade_id="t1", price=0.5, size=10.0)  # value 5
        _trade(db, trade_id="t2", condition_id="c2", price=0.5, size=30.0)  # value 15
        db.commit()
        result = analytics.get_wallet_intelligence_summary(db, _ADDR)
        assert result["average_trade_size"] == pytest.approx(10.0)  # (5+15)/2


class TestBuildActivityHeatmap:
    def test_returns_exactly_days_entries(self, db):
        result = analytics.build_activity_heatmap(db)
        assert len(result) == 7

    def test_empty_db_all_zeros(self, db):
        result = analytics.build_activity_heatmap(db)
        assert all(day["count"] == 0 for day in result)
        assert all(day["bar_pct"] == 0 for day in result)

    def test_trade_today_shows_in_last_entry(self, db):
        _wallet(db)
        _trade(db, hours_ago=0)
        db.commit()
        result = analytics.build_activity_heatmap(db)
        today_entry = result[-1]
        assert today_entry["count"] >= 1

    def test_wallet_scoped_heatmap(self, db):
        _wallet(db, _ADDR)
        _wallet(db, _ADDR2)
        _trade(db, _ADDR, "t-a1")
        _trade(db, _ADDR2, "t-b1", condition_id="c-b")
        db.commit()
        result_a = analytics.build_activity_heatmap(db, wallet_address=_ADDR)
        result_all = analytics.build_activity_heatmap(db)
        count_a = sum(d["count"] for d in result_a)
        count_all = sum(d["count"] for d in result_all)
        assert count_a == 1
        assert count_all == 2

    def test_bar_pct_max_day_is_100(self, db):
        _wallet(db)
        for i in range(5):
            _trade(db, trade_id=f"pct-{i}", condition_id=f"c-{i}")
        db.commit()
        result = analytics.build_activity_heatmap(db)
        assert max(d["bar_pct"] for d in result) == 100


class TestBuildTopMarkets:
    def test_empty_returns_empty_list(self, db):
        assert analytics.build_top_markets(db) == []

    def test_orders_by_total_value_descending(self, db):
        _wallet(db)
        _trade(db, trade_id="m1", condition_id="cond-small", price=0.1, size=1.0)
        _trade(db, trade_id="m2", condition_id="cond-large", price=0.9, size=100.0)
        db.commit()
        result = analytics.build_top_markets(db)
        assert result[0]["condition_id"] == "cond-large"
        assert result[0]["bar_pct"] == 100

    def test_wallet_scoped(self, db):
        _wallet(db, _ADDR)
        _wallet(db, _ADDR2)
        _trade(db, _ADDR, "own-1", "cond-own")
        _trade(db, _ADDR2, "other-1", "cond-other")
        db.commit()
        result = analytics.build_top_markets(db, wallet_address=_ADDR)
        assert len(result) == 1
        assert result[0]["condition_id"] == "cond-own"

    def test_all_yes_trades(self, db):
        _wallet(db)
        for i in range(3):
            _trade(db, trade_id=f"yes-{i}", condition_id=f"cond-yes-{i}", side="YES", price=0.5, size=10.0)
        db.commit()
        result = analytics.build_top_markets(db)
        assert len(result) == 3
        assert all(r["trade_count"] == 1 for r in result)

    def test_all_no_trades(self, db):
        _wallet(db)
        _trade(db, side="NO", price=0.5, size=20.0)
        db.commit()
        result = analytics.build_top_markets(db)
        assert result[0]["total_value"] == pytest.approx(10.0)


class TestBuildWalletActivityTimeline:
    def test_empty_wallet_returns_empty_list(self, db):
        _wallet(db)
        db.commit()
        result = analytics.build_wallet_activity_timeline(db, _ADDR)
        assert result == []

    def test_includes_trade_and_sync_events(self, db):
        _wallet(db)
        _trade(db)
        db.add(SyncEvent(wallet_address=_ADDR, status="success", fetched_count=1, inserted_count=1))
        db.commit()
        result = analytics.build_wallet_activity_timeline(db, _ADDR)
        kinds = {item["kind"] for item in result}
        assert "trade" in kinds
        assert "sync" in kinds

    def test_ordered_newest_first(self, db):
        _wallet(db)
        _trade(db, trade_id="older", hours_ago=2)
        _trade(db, trade_id="newer", condition_id="c2", hours_ago=0)
        db.commit()
        result = analytics.build_wallet_activity_timeline(db, _ADDR)
        trade_items = [r for r in result if r["kind"] == "trade"]
        assert trade_items[0]["href"].endswith("newer")
