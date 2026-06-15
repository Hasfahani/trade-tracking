# Tests shared helper functions.
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import alerts
from app import view_helpers as vh
from app.models import AppSettings, Base, Trade, Wallet
from app.routes import _shared


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture()
def db(session_factory):
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


def _wallet(address: str, label=None, tags=None, notes=None):
    return Wallet(address=address, label=label, tags=tags, notes=notes)


def _trade(
    wallet_address: str,
    trade_id: str,
    condition_id: str,
    *,
    market_title="Market",
    side="YES",
    price=0.5,
    size=10,
    traded_at=None,
):
    return Trade(
        wallet_address=wallet_address,
        trade_id=trade_id,
        condition_id=condition_id,
        market_title=market_title,
        side=side,
        price=price,
        size=size,
        traded_at=traded_at or datetime.now(timezone.utc).replace(tzinfo=None),
    )


def test_formatting_helpers_handle_short_long_and_empty_values():
    assert vh.short_address("0x123") == "0x123"
    assert vh.short_address("0x1234567890abcdef1234567890abcdef12345678") == "0x123456...345678"
    assert vh.duration_label(None) == "-"
    assert vh.duration_label(999) == "999 ms"
    assert vh.duration_label(1500) == "1.5 s"
    assert vh.normalize_tags("desk, whales, desk\nOps|ops") == "desk, whales, Ops"
    assert vh.tag_list(None) == []
    assert vh.tag_list("alpha, beta,,") == ["alpha", "beta"]
    assert vh.validate_wallet_address("") == "Wallet address is required."
    assert vh.validate_wallet_address("bad") == "Wallet address must be a valid 42-character hex address starting with 0x."
    assert vh.validate_wallet_address("0x" + "a" * 40) is None


def test_wallet_label_address_search_helper_matches_label_and_address(db):
    alpha = _wallet("0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", label="Alpha Desk")
    beta = _wallet("0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", label=None, notes="quiet wallet")
    db.add_all([alpha, beta])
    db.add_all(
        [
            _trade(alpha.address, "alpha-trade", "cond-a"),
            _trade(beta.address, "beta-trade", "cond-b"),
        ]
    )
    db.commit()

    label_matches = vh.apply_wallet_search_to_trade_query(db, db.query(Trade), "alpha").all()
    address_matches = vh.apply_wallet_search_to_trade_query(db, db.query(Trade), "bbbb").all()
    no_search = vh.apply_wallet_search_to_trade_query(db, db.query(Trade), None).count()

    assert [trade.trade_id for trade in label_matches] == ["alpha-trade"]
    assert [trade.trade_id for trade in address_matches] == ["beta-trade"]
    assert no_search == 2


def test_wallet_query_search_covers_missing_labels_tags_and_notes(db):
    unlabeled = _wallet("0xcccccccccccccccccccccccccccccccccccccccc", label=None, tags=None, notes=None)
    tagged = _wallet("0xdddddddddddddddddddddddddddddddddddddddd", label=None, tags="Strategy", notes=None)
    db.add_all([unlabeled, tagged])
    db.commit()

    assert vh.build_wallet_query(db, wallet_search="cccc").one().address == unlabeled.address
    assert vh.build_wallet_query(db, wallet_search="strategy").one().address == tagged.address
    assert vh.build_wallet_query(db, wallet_search="missing").count() == 0


def test_pagination_meta_handles_empty_middle_and_last_pages():
    assert vh.pagination_meta(1, 25, 0) == {"start": 0, "end": 0}
    assert vh.pagination_meta(1, 25, 26) == {"start": 1, "end": 25}
    assert vh.pagination_meta(2, 25, 26) == {"start": 26, "end": 26}


def test_shared_date_and_query_pagination_helpers(db):
    wallet = _wallet("0x4444444444444444444444444444444444444444")
    db.add(wallet)
    db.add_all(
        [
            _trade(wallet.address, "page-1", "cond-page", traded_at=datetime(2026, 1, 1, 12, 0)),
            _trade(wallet.address, "page-2", "cond-page", traded_at=datetime(2026, 1, 2, 12, 0)),
            _trade(wallet.address, "page-3", "cond-page", traded_at=datetime(2026, 1, 3, 12, 0)),
        ]
    )
    db.commit()

    date_from, date_to = _shared.normalized_date_filters("7d", None, None)
    explicit_from, explicit_to = _shared.normalized_date_filters("7d", "2026-01-01", None)
    page, total_items, total_pages, pagination, items = _shared.paginated_query(
        db.query(Trade).order_by(Trade.traded_at.asc()),
        page=99,
        page_size=2,
    )

    assert date_from is not None
    assert date_to is not None
    assert (explicit_from, explicit_to) == ("2026-01-01", None)
    assert page == 2
    assert total_items == 3
    assert total_pages == 2
    assert pagination == {"start": 3, "end": 3}
    assert [item.trade_id for item in items] == ["page-3"]


def test_shared_route_helpers_resolve_wallet_and_guard_next(db):
    wallet = _wallet("0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee", label="Edge")
    db.add(wallet)
    db.commit()
    db.refresh(wallet)

    assert _shared.resolve_wallet(db, str(wallet.id)).address == wallet.address
    assert _shared.resolve_wallet(db, wallet.address.upper()).address == wallet.address
    assert _shared._safe_next("/all-trades") == "/all-trades"
    assert _shared._safe_next("/wallets/123/trades") == "/wallets/123/trades"
    assert _shared._safe_next("https://example.com") is None
    with pytest.raises(HTTPException):
        _shared.resolve_wallet(db, "0xffffffffffffffffffffffffffffffffffffffff")


def test_alert_message_uses_label_market_fallback_and_zero_price():
    wallet = _wallet("0xffffffffffffffffffffffffffffffffffffffff", label=None)
    trade = _trade(
        wallet.address,
        "alert-trade",
        "cond-alert",
        market_title=None,
        side="NO",
        price=0.0,
        size=100,
        traded_at=datetime(2026, 1, 1, 12, 30),
    )

    message = alerts._build_message(trade, wallet)

    assert "0xffff" in message
    assert "cond-alert" in message
    assert "SELL" in message
    assert "$0.00" in message


def test_fire_alerts_empty_data_returns_zero(db):
    wallet = _wallet("0x1111111111111111111111111111111111111111")
    db.add(wallet)
    db.add(AppSettings(id=1, alerts_enabled=1, telegram_bot_token="token", telegram_chat_id="chat", alert_min_size=1))
    db.commit()

    assert alerts.fire_alerts_for_new_trades(db, wallet) == 0


def test_detect_interesting_activity_empty_data(db):
    assert vh.detect_interesting_activity(db) == []


def test_detect_interesting_activity_detects_large_spike_and_new_market_without_n_plus_one(db):
    address = "0x2222222222222222222222222222222222222222"
    unlabeled = "0x3333333333333333333333333333333333333333"
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add_all([_wallet(address, label="Signal"), _wallet(unlabeled, label=None)])
    db.add_all(
        [
            _trade(address, "large-1", "cond-large", market_title="Large Market", price=0.8, size=300, traded_at=now),
            _trade(address, "spike-1", "cond-spike", price=0.1, size=10, traded_at=now - timedelta(minutes=1)),
            _trade(address, "spike-2", "cond-spike", price=0.1, size=10, traded_at=now - timedelta(minutes=2)),
            _trade(address, "spike-3", "cond-spike", price=0.1, size=10, traded_at=now - timedelta(minutes=3)),
            _trade(unlabeled, "new-1", "cond-new", market_title=None, price=0.0, size=100, traded_at=now - timedelta(minutes=4)),
            _trade(address, "old-market", "cond-existing", price=0.5, size=10, traded_at=now - timedelta(days=2)),
            _trade(address, "recent-existing", "cond-existing", price=0.5, size=10, traded_at=now - timedelta(minutes=5)),
        ]
    )
    db.commit()

    query_count = 0

    def count_queries(*args, **kwargs):
        nonlocal query_count
        query_count += 1

    event.listen(db.bind, "before_cursor_execute", count_queries)
    try:
        events = vh.detect_interesting_activity(db)
    finally:
        event.remove(db.bind, "before_cursor_execute", count_queries)

    event_types = {event["type"] for event in events}
    large = next(event for event in events if event["type"] == "large_trade")
    spike = next(event for event in events if event["type"] == "activity_spike")
    new_markets = [event for event in events if event["type"] == "new_market"]

    assert {"large_trade", "activity_spike", "new_market"} <= event_types
    assert large["label"] == "Signal"
    assert spike["count"] == 5
    assert any(event["wallet"] == unlabeled and event["label"] == vh.short_address(unlabeled) for event in new_markets)
    assert not any(event.get("market") == "cond-existing" for event in new_markets)
    assert query_count <= 3
