# Summary: Tests wallet discovery aggregation and watchlist insertion.
# Details: It checks this part of the project so future code changes do not silently break expected behavior.
"""Tests for app.discovery: feed aggregation/ranking and idempotent insertion."""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.discovery as discovery
from app.discovery import (
    add_discovered_wallets,
    discover_active_wallets,
    discover_leaderboard_wallets,
    tracked_addresses,
)
from app.models import Base, Wallet

W1 = "0x" + "1" * 40
W2 = "0x" + "2" * 40
W3 = "0x" + "3" * 40


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _trade(wallet, price, size, condition_id, name=""):
    return {"proxyWallet": wallet, "price": price, "size": size,
            "conditionId": condition_id, "name": name}


def test_discover_ranks_by_volume_and_aggregates(monkeypatch):
    page = [
        _trade(W1, 0.5, 100, "c1", "Alice"),   # W1 volume 50
        _trade(W1, 0.5, 100, "c2", "Alice"),   # +50 -> 100, 2 markets
        _trade(W2, 0.9, 100, "c1", "Bob"),     # W2 volume 90, 1 market
    ]
    monkeypatch.setattr(discovery, "_fetch_feed_page", lambda limit, offset: page if offset == 0 else [])

    result = discover_active_wallets(pages=3, page_size=500)

    assert [w.address for w in result] == [W1, W2]  # 100 > 90
    w1 = result[0]
    assert w1.volume == 100.0 and w1.trade_count == 2 and w1.market_count == 2
    assert w1.name == "Alice"


def test_discover_excludes_known_addresses(monkeypatch):
    page = [_trade(W1, 0.5, 100, "c1"), _trade(W2, 0.5, 100, "c1")]
    monkeypatch.setattr(discovery, "_fetch_feed_page", lambda limit, offset: page if offset == 0 else [])

    result = discover_active_wallets(pages=1, exclude={W1.upper()})  # case-insensitive

    assert [w.address for w in result] == [W2]


def test_add_discovered_is_idempotent_and_respects_thresholds():
    db = _session()
    db.add(Wallet(address=W1))  # already tracked
    db.commit()

    discovered = [
        discovery.DiscoveredWallet(W1, "Alice", 100.0, 5, 2),   # existing -> skip
        discovery.DiscoveredWallet(W2, "Bob", 100.0, 5, 2),     # added
        discovery.DiscoveredWallet(W3, "Carl", 1.0, 1, 1),      # below volume threshold
        discovery.DiscoveredWallet("not-an-address", "X", 999.0, 9, 9),  # invalid
    ]
    result = add_discovered_wallets(db, discovered, min_volume=10.0, min_trades=1)

    assert result["added"] == [W2]
    assert result["skipped_existing"] == 1
    assert result["skipped_threshold"] == 1
    assert result["skipped_invalid"] == 1
    assert tracked_addresses(db) == {W1, W2}

    # Re-running adds nothing new.
    again = add_discovered_wallets(db, discovered, min_volume=10.0, min_trades=1)
    assert again["added"] == []


def test_max_add_caps_insertions():
    db = _session()
    discovered = [
        discovery.DiscoveredWallet(W1, "A", 100.0, 5, 2),
        discovery.DiscoveredWallet(W2, "B", 100.0, 5, 2),
        discovery.DiscoveredWallet(W3, "C", 100.0, 5, 2),
    ]
    result = add_discovered_wallets(db, discovered, max_add=2)
    assert result["added_count"] == 2


def _lb_row(wallet, amount, name=""):
    return {"proxyWallet": wallet, "amount": amount, "name": name, "pseudonym": name}


def test_discover_leaderboard_profit_preserves_rank_and_sets_profit(monkeypatch):
    rows = [_lb_row(W1, 1_000_000.0, "Whale"), _lb_row(W2, 500_000.0, "Shark")]
    monkeypatch.setattr(discovery, "_fetch_leaderboard", lambda kind, window, limit: rows)

    result = discover_leaderboard_wallets(kind="profit", window="all", limit=10)

    assert [w.address for w in result] == [W1, W2]  # API rank order preserved
    top = result[0]
    assert top.profit == 1_000_000.0 and top.volume == 0.0
    assert top.source == "leaderboard:profit"
    assert "realized profit" in top.note and "all-time" in top.note


def test_discover_leaderboard_volume_sets_volume_not_profit(monkeypatch):
    monkeypatch.setattr(
        discovery, "_fetch_leaderboard", lambda kind, window, limit: [_lb_row(W1, 9.0, "V")]
    )
    result = discover_leaderboard_wallets(kind="volume", window="7d", limit=10)
    assert result[0].volume == 9.0 and result[0].profit == 0.0
    assert result[0].source == "leaderboard:volume" and "7d" in result[0].note


def test_discover_leaderboard_excludes_and_dedups(monkeypatch):
    rows = [_lb_row(W1, 3.0), _lb_row(W2, 2.0), _lb_row(W1, 1.0)]  # W1 duplicated
    monkeypatch.setattr(discovery, "_fetch_leaderboard", lambda kind, window, limit: rows)
    result = discover_leaderboard_wallets(exclude={W1.upper()})  # case-insensitive
    assert [w.address for w in result] == [W2]


def test_discover_leaderboard_normalizes_bad_kind_and_window(monkeypatch):
    captured = {}

    def fake(kind, window, limit):
        captured["kind"], captured["window"] = kind, window
        return []

    monkeypatch.setattr(discovery, "_fetch_leaderboard", fake)
    discover_leaderboard_wallets(kind="bogus", window="bogus", limit=5)
    assert captured == {"kind": "profit", "window": "all"}


def test_add_leaderboard_wallet_uses_note_and_allows_zero_trades():
    db = _session()
    wallet = discovery.DiscoveredWallet(
        W2, "Whale", volume=0.0, trade_count=0, market_count=0,
        profit=1_000_000.0, source="leaderboard:profit",
        note="Top-profit leaderboard wallet: ~$1,000,000 realized profit (all-time).",
    )
    # min_trades=0 because leaderboard candidates carry no per-wallet trade count.
    result = add_discovered_wallets(db, [wallet], min_trades=0, tag="profitable")
    assert result["added"] == [W2]
    row = db.query(Wallet).filter(Wallet.address == W2).one()
    assert row.label == "Whale (top profit)"
    assert "realized profit" in row.notes
    assert row.tags == "profitable"
