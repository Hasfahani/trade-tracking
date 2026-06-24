# Tests that AI analysis is grounded in real resolved-market performance.
"""Phase 3: AI accuracy + reach.

The trade context and prompts now include the wallet's realized track record
(win rate / ROI from resolved markets), and "Analyze with AI" deep links appear
on the trades tables and dashboard activity.
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai_analysis import build_trade_context, _build_prompt
from app.db import get_db
from app.main import create_app
from app.models import Base, MarketResolution, Trade, Wallet

_ADDR = "0xcccccccccccccccccccccccccccccccccccccccc"


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _seed_resolved(factory):
    db = factory()
    try:
        db.add(Wallet(address=_ADDR))
        ts = datetime(2026, 1, 1, 12, 0)
        # c1 (YES wins): buy 10 YES @0.40 -> +6.0 ; c2 (NO wins): buy 5 YES @0.50 -> -2.5
        db.add(Trade(wallet_address=_ADDR, trade_id="t1", condition_id="c1", market_title="M1",
                     side="YES", outcome_token="YES", price=0.40, size=10.0, traded_at=ts))
        db.add(Trade(wallet_address=_ADDR, trade_id="t2", condition_id="c2", market_title="M2",
                     side="YES", outcome_token="YES", price=0.50, size=5.0, traded_at=ts))
        db.add(MarketResolution(condition_id="c1", outcome="YES"))
        db.add(MarketResolution(condition_id="c2", outcome="NO"))
        db.commit()
    finally:
        db.close()


class TestContextGrounding:
    def test_context_includes_real_track_record(self):
        factory = _session()
        _seed_resolved(factory)
        db = factory()
        try:
            trade = db.query(Trade).filter(Trade.trade_id == "t1").first()
            ctx = build_trade_context(trade, db)
        finally:
            db.close()
        assert ctx["wallet_has_track_record"] is True
        assert ctx["wallet_markets_won"] == 1
        assert ctx["wallet_markets_lost"] == 1
        assert ctx["wallet_win_rate_pct"] == 50
        assert ctx["wallet_roi_pct"] is not None

    def test_context_without_resolutions_has_no_track_record(self):
        factory = _session()
        db = factory()
        try:
            db.add(Wallet(address=_ADDR))
            db.add(Trade(wallet_address=_ADDR, trade_id="t1", condition_id="c1", market_title="M1",
                         side="YES", outcome_token="YES", price=0.40, size=10.0,
                         traded_at=datetime(2026, 1, 1)))
            db.commit()
            trade = db.query(Trade).filter(Trade.trade_id == "t1").first()
            ctx = build_trade_context(trade, db)
        finally:
            db.close()
        assert ctx["wallet_has_track_record"] is False
        assert ctx["wallet_win_rate_pct"] is None

    def test_prompt_mentions_track_record(self):
        factory = _session()
        _seed_resolved(factory)
        db = factory()
        try:
            trade = db.query(Trade).filter(Trade.trade_id == "t1").first()
            ctx = build_trade_context(trade, db)
            prompt = _build_prompt(trade, ctx)
        finally:
            db.close()
        assert "TRACK RECORD" in prompt
        assert "win rate" in prompt

    def test_prompt_omits_track_record_without_data(self):
        factory = _session()
        db = factory()
        try:
            db.add(Wallet(address=_ADDR))
            db.add(Trade(wallet_address=_ADDR, trade_id="t1", condition_id="c1", market_title="M1",
                         side="YES", outcome_token="YES", price=0.40, size=10.0,
                         traded_at=datetime(2026, 1, 1)))
            db.commit()
            trade = db.query(Trade).filter(Trade.trade_id == "t1").first()
            prompt = _build_prompt(trade, build_trade_context(trade, db))
        finally:
            db.close()
        assert "TRACK RECORD" not in prompt


class TestAnalyzeDeepLinks:
    def _client(self):
        factory = _session()
        app = create_app(lifespan_context=None, csrf_enabled=False)

        def override():
            db = factory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override
        return TestClient(app), factory

    def _seed_recent(self, factory):
        db = factory()
        try:
            db.add(Wallet(address=_ADDR))
            recent = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
            # Large trade so it appears in dashboard "interesting activity".
            db.add(Trade(wallet_address=_ADDR, trade_id="big-1", condition_id="c1", market_title="Big Market",
                         side="YES", outcome_token="YES", price=0.50, size=1000.0, traded_at=recent))
            db.commit()
        finally:
            db.close()

    def test_all_trades_has_analyze_link(self):
        client, factory = self._client()
        self._seed_recent(factory)
        r = client.get("/all-trades")
        assert r.status_code == 200
        assert "/trades/big-1?analyze=1#ai-section" in r.text

    def test_wallet_trades_has_analyze_button(self):
        client, factory = self._client()
        self._seed_recent(factory)
        r = client.get(f"/wallets/{_ADDR}/trades")
        assert r.status_code == 200
        assert "?analyze=1#ai-section" in r.text
        assert "Analyze with AI" in r.text

    def test_dashboard_activity_has_analyze_link(self):
        client, factory = self._client()
        self._seed_recent(factory)
        r = client.get("/dashboard")
        assert r.status_code == 200
        assert "analyze=1#ai-section" in r.text
