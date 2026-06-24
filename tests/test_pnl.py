# Tests realized PnL/ROI/win-rate from resolved markets.
"""Phase 2 tests: honest resolved-market performance.

Covers the pure PnL engine (win/lose/mixed/unresolved/buy+sell), the DB-backed
per-wallet map, market-resolution parsing/upsert, refresh resilience when the
resolution fetch fails, and the wallet/leaderboard UI showing real numbers vs
the honest placeholder.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.analytics import (
    compute_performance_from_trades,
    compute_wallet_performance,
    compute_wallet_performance_map,
)
from app.db import get_db
from app.ingest import (
    normalize_trade,
    parse_market_resolution,
    refresh_wallet,
    upsert_market_resolution,
)
from app.main import create_app
from app.models import Base, MarketResolution, Trade, Wallet

_ADDR = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
_CID = "0x" + "a" * 64  # a real-looking condition id


def _t(side, token, price, size, condition_id="c"):
    return SimpleNamespace(
        condition_id=condition_id, side=side, outcome_token=token, price=price, size=size
    )


class TestPnlEngine:
    def test_winning_buy(self):
        r = compute_performance_from_trades([_t("YES", "YES", 0.45, 10)], {"c": "YES"})
        assert r["realized_pnl"] == pytest.approx(5.5)  # 10 * (1 - 0.45)
        assert r["roi"] == pytest.approx(5.5 / 4.5)
        assert (r["markets_won"], r["markets_lost"]) == (1, 0)
        assert r["win_rate"] == pytest.approx(1.0)
        assert r["has_data"] is True

    def test_losing_buy(self):
        r = compute_performance_from_trades([_t("YES", "YES", 0.45, 10)], {"c": "NO"})
        assert r["realized_pnl"] == pytest.approx(-4.5)
        assert r["roi"] == pytest.approx(-1.0)
        assert (r["markets_won"], r["markets_lost"]) == (0, 1)

    def test_buy_then_partial_sell_held_to_resolution(self):
        # Buy 10 @0.40, sell 4 @0.60, YES wins: -4.0 + 2.4 + 6 = 4.4
        trades = [_t("YES", "YES", 0.40, 10), _t("NO", "YES", 0.60, 4)]
        r = compute_performance_from_trades(trades, {"c": "YES"})
        assert r["realized_pnl"] == pytest.approx(4.4)

    def test_buying_the_no_token_that_wins(self):
        r = compute_performance_from_trades([_t("YES", "NO", 0.30, 10)], {"c": "NO"})
        assert r["realized_pnl"] == pytest.approx(7.0)  # 10 * (1 - 0.30)

    def test_unresolved_market_is_excluded(self):
        r = compute_performance_from_trades([_t("YES", "YES", 0.45, 10)], {})
        assert r["has_data"] is False
        assert r["resolved_markets"] == 0

    def test_trades_without_outcome_token_are_ignored(self):
        r = compute_performance_from_trades([_t("YES", None, 0.45, 10)], {"c": "YES"})
        assert r["has_data"] is False

    def test_mixed_markets_win_rate(self):
        trades = [
            _t("YES", "YES", 0.40, 10, condition_id="c1"),  # wins +6.0
            _t("YES", "YES", 0.50, 5, condition_id="c2"),   # loses -2.5
        ]
        r = compute_performance_from_trades(trades, {"c1": "YES", "c2": "NO"})
        assert r["realized_pnl"] == pytest.approx(3.5)
        assert (r["markets_won"], r["markets_lost"]) == (1, 1)
        assert r["win_rate"] == pytest.approx(0.5)
        assert r["resolved_trades"] == 2


class TestResolutionParsing:
    def test_closed_market_with_no_winner(self):
        market = {
            "question": "Will X happen?",
            "closed": True,
            "end_date_iso": "2026-04-18T00:00:00Z",
            "tokens": [
                {"outcome": "Yes", "winner": False},
                {"outcome": "No", "winner": True},
            ],
        }
        r = parse_market_resolution(market, _CID)
        assert r["outcome"] == "NO"
        assert r["resolved_at"] == datetime(2026, 4, 18, 0, 0)
        assert r["market_title"] == "Will X happen?"

    def test_open_market_is_unresolved(self):
        r = parse_market_resolution({"closed": False, "tokens": []}, _CID)
        assert r["outcome"] == "UNRESOLVED"
        assert r["resolved_at"] is None

    def test_closed_but_ambiguous_is_unresolved(self):
        market = {"closed": True, "tokens": [{"outcome": "Yes", "winner": True}, {"outcome": "No", "winner": True}]}
        assert parse_market_resolution(market, _CID)["outcome"] == "UNRESOLVED"

    def test_garbage_payload(self):
        assert parse_market_resolution("not a dict", _CID) is None


class TestDbBacked:
    @pytest.fixture()
    def db(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(bind=engine)
        session = sessionmaker(bind=engine)()
        try:
            yield session
        finally:
            session.close()

    def _seed(self, db):
        db.add(Wallet(address=_ADDR))
        ts = datetime(2026, 1, 1, 12, 0)
        # Market c1 (YES wins): buy 10 YES @0.40 -> +6.0
        db.add(Trade(wallet_address=_ADDR, trade_id="t1", condition_id="c1", side="YES",
                     outcome_token="YES", price=0.40, size=10.0, traded_at=ts))
        # Market c2 (NO wins): buy 5 YES @0.50 -> -2.5
        db.add(Trade(wallet_address=_ADDR, trade_id="t2", condition_id="c2", side="YES",
                     outcome_token="YES", price=0.50, size=5.0, traded_at=ts))
        db.add(MarketResolution(condition_id="c1", outcome="YES"))
        db.add(MarketResolution(condition_id="c2", outcome="NO"))
        db.commit()

    def test_compute_wallet_performance(self, db):
        self._seed(db)
        perf = compute_wallet_performance(db, _ADDR)
        assert perf["has_data"] is True
        assert perf["realized_pnl"] == pytest.approx(3.5)
        assert perf["roi"] == pytest.approx(3.5 / 6.5)
        assert (perf["markets_won"], perf["markets_lost"]) == (1, 1)

    def test_map_excludes_unresolved_only_wallets(self, db):
        db.add(Wallet(address=_ADDR))
        db.add(Trade(wallet_address=_ADDR, trade_id="t1", condition_id="c9", side="YES",
                     outcome_token="YES", price=0.5, size=10.0, traded_at=datetime(2026, 1, 1)))
        db.commit()  # no resolution rows
        assert compute_wallet_performance_map(db) == {}
        assert compute_wallet_performance(db, _ADDR)["has_data"] is False

    def test_upsert_market_resolution_is_idempotent(self, db):
        data = {"condition_id": _CID, "market_title": "Q", "outcome": "YES", "resolved_at": None}
        upsert_market_resolution(db, data)
        db.commit()
        data["outcome"] = "NO"
        upsert_market_resolution(db, data)
        db.commit()
        rows = db.query(MarketResolution).all()
        assert len(rows) == 1
        assert rows[0].outcome == "NO"


class TestNormalizeCapturesOutcomeToken:
    def test_outcome_token_captured(self):
        raw = {"id": "x", "conditionId": "cond", "side": "BUY", "outcome": "No", "price": 0.5, "size": 10, "timestamp": 1713139200}
        result = normalize_trade(raw, _ADDR)
        assert result["side"] == "YES"  # BUY -> YES (action), preserved
        assert result["outcome_token"] == "NO"  # the Yes/No token captured


class TestRefreshResilience:
    @pytest.fixture()
    def db(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(bind=engine)
        session = sessionmaker(bind=engine)()
        try:
            yield session
        finally:
            session.close()

    def test_refresh_succeeds_when_resolution_fetch_raises(self, db):
        wallet = Wallet(address=_ADDR)
        db.add(wallet)
        db.commit()
        raw = {"id": "r1", "conditionId": _CID, "title": "M", "side": "BUY",
               "outcome": "Yes", "price": 0.5, "size": 10.0, "timestamp": 1713139200}
        with patch("app.ingest.fetch_trades_for_wallet", return_value=[raw]), \
             patch("app.ingest.refresh_resolutions_for_wallet", side_effect=RuntimeError("boom")):
            result = refresh_wallet(db, wallet)
        assert result["status"] == "success"
        assert result["inserted"] == 1
        # The captured outcome token survives the failed resolution step.
        stored = db.query(Trade).filter(Trade.trade_id == "r1").first()
        assert stored.outcome_token == "YES"


class TestPerformanceUI:
    def _client(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(bind=engine)
        factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        app = create_app(lifespan_context=None, csrf_enabled=False)

        def override():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override
        return TestClient(app), factory

    def _seed(self, factory):
        db = factory()
        try:
            db.add(Wallet(address=_ADDR))
            ts = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
            db.add(Trade(wallet_address=_ADDR, trade_id="t1", condition_id="c1", side="YES",
                         outcome_token="YES", price=0.40, size=10.0, traded_at=ts))
            db.add(Trade(wallet_address=_ADDR, trade_id="t2", condition_id="c2", side="YES",
                         outcome_token="YES", price=0.50, size=5.0, traded_at=ts))
            db.add(MarketResolution(condition_id="c1", outcome="YES"))
            db.add(MarketResolution(condition_id="c2", outcome="NO"))
            db.commit()
        finally:
            db.close()

    def test_wallet_page_shows_real_performance(self):
        client, factory = self._client()
        self._seed(factory)
        response = client.get(f"/wallets/{_ADDR}")
        assert response.status_code == 200
        assert "Resolved-market performance" in response.text
        assert "1 won / 1 lost" in response.text
        assert "Not enough resolved market data yet" not in response.text

    def test_wallet_page_shows_placeholder_without_resolutions(self):
        client, factory = self._client()
        db = factory()
        try:
            db.add(Wallet(address=_ADDR))
            db.add(Trade(wallet_address=_ADDR, trade_id="t1", condition_id="c1", side="YES",
                         outcome_token="YES", price=0.40, size=10.0,
                         traded_at=datetime.now(timezone.utc).replace(tzinfo=None)))
            db.commit()
        finally:
            db.close()
        response = client.get(f"/wallets/{_ADDR}")
        assert response.status_code == 200
        assert "Not enough resolved market data yet" in response.text

    def test_leaderboard_shows_real_roi(self):
        client, factory = self._client()
        self._seed(factory)
        response = client.get("/leaderboard")
        assert response.status_code == 200
        assert "53.8%" in response.text  # ROI = 3.5 / 6.5
