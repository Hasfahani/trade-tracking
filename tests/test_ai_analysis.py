# Summary: Tests AI trade analysis.
# Details: It checks this part of the project so future code changes do not silently break expected behavior.
"""Tests for app/ai_analysis.py â€” provider detection, parsing, caching, and DB persistence."""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai_analysis import (
    _parse_response,
    _load_cached_analysis,
    _save_analysis_to_db,
    _load_cached_wallet_summary,
    _save_wallet_summary,
    build_trade_context,
    invalidate_cache,
    invalidate_wallet_summary,
    analyze_trade,
    get_trade_summary,
)
from app.models import Base, Trade, TradeAnalysis, Wallet


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
_NOW = datetime(2026, 1, 15, 12, 0, 0)


def _wallet(db, address=_ADDR, label="Test Wallet"):
    w = Wallet(address=address, label=label)
    db.add(w)
    db.flush()
    return w


def _trade(
    db,
    trade_id="t1",
    condition_id="cond-1",
    *,
    side="YES",
    price=0.65,
    size=100.0,
    address=_ADDR,
    market_title="Will it rain?",
    hours_ago=0,
    notable_score=None,
):
    t = Trade(
        wallet_address=address,
        trade_id=trade_id,
        condition_id=condition_id,
        market_title=market_title,
        side=side,
        price=price,
        size=size,
        traded_at=_NOW - timedelta(hours=hours_ago),
        notable_score=notable_score,
    )
    db.add(t)
    db.flush()
    return t


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

class TestParseResponse:
    def test_parses_all_fields(self):
        text = (
            "SIGNAL: CONTRARIAN\n"
            "RISK: HIGH\n"
            "PRICE_INSIGHT: Trader implies 75% YES probability, well above market average.\n"
            "BEHAVIOR: Wallet consistently bets against consensus on high-liquidity markets.\n"
            "VERDICT: Aggressive contrarian bet with outsized conviction."
        )
        result = _parse_response(text)
        assert result["signal"] == "CONTRARIAN"
        assert result["risk"] == "HIGH"
        assert "75%" in result["price_insight"]
        assert result["behavior"] is not None
        assert result["verdict"] is not None

    def test_case_insensitive_labels(self):
        text = "signal: CONVICTION\nrisk: low\nPRICE_INSIGHT: test\nBEHAVIOR: test\nVERDICT: test"
        result = _parse_response(text)
        assert result["signal"] == "CONVICTION"
        assert result["risk"] == "LOW"

    def test_invalid_signal_falls_back_to_inference(self):
        result = _parse_response("SIGNAL: UNKNOWN\nThis trade shows high conviction sizing.")
        assert result["signal"] == "CONVICTION"

    def test_empty_text_returns_none_fields(self):
        result = _parse_response("")
        assert result["signal"] is None
        assert result["risk"] is None
        assert result["verdict"] is None

    def test_contrarian_keyword_fallback(self):
        result = _parse_response("This is a very contrarian position in the market.")
        assert result["signal"] == "CONTRARIAN"

    def test_consensus_keyword_fallback(self):
        result = _parse_response("Follows momentum with strong consensus.")
        assert result["signal"] == "CONSENSUS"

    def test_speculative_default_fallback(self):
        result = _parse_response("Something unclear happened.")
        assert result["signal"] == "SPECULATIVE"

    def test_valid_signals_accepted(self):
        for sig in ("CONTRARIAN", "CONSENSUS", "CONVICTION", "SPECULATIVE"):
            result = _parse_response(f"SIGNAL: {sig}\nRISK: LOW\nPRICE_INSIGHT: x\nBEHAVIOR: x\nVERDICT: x")
            assert result["signal"] == sig

    def test_invalid_risk_not_stored(self):
        result = _parse_response("SIGNAL: CONVICTION\nRISK: EXTREME\nPRICE_INSIGHT: x\nBEHAVIOR: x\nVERDICT: x")
        assert result["risk"] is None


# ---------------------------------------------------------------------------
# DB cache â€” trade analysis
# ---------------------------------------------------------------------------

class TestTradeAnalysisCache:
    def test_load_returns_none_when_missing(self, db):
        assert _load_cached_analysis("nonexistent", db) is None

    def test_save_and_load_roundtrip(self, db):
        _wallet(db)
        _trade(db)
        result = {
            "signal": "CONVICTION",
            "risk": "HIGH",
            "price_insight": "Implies 65% YES probability.",
            "behavior": "Oversized bet for this wallet.",
            "verdict": "Whale entry.",
            "provider": "Claude (claude-sonnet-4-6)",
            "model_version": "claude-sonnet-4-6",
        }
        ctx = {"trade_value": 65.0, "market_yes_pct": 55}
        _save_analysis_to_db("t1", result, ctx, db)

        loaded = _load_cached_analysis("t1", db)
        assert loaded is not None
        assert loaded["signal"] == "CONVICTION"
        assert loaded["risk"] == "HIGH"
        assert loaded["_from_cache"] is True
        assert loaded["context"]["trade_value"] == 65.0

    def test_expired_cache_returns_none(self, db):
        _wallet(db)
        _trade(db)
        result = {"signal": "SPECULATIVE", "risk": "LOW", "price_insight": "x", "behavior": "x", "verdict": "x", "provider": "test", "model_version": "test"}
        _save_analysis_to_db("t1", result, {}, db)

        row = db.query(TradeAnalysis).filter(TradeAnalysis.trade_id == "t1").first()
        row.created_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=200)
        db.commit()

        with patch("app.ai_analysis.settings") as mock_settings:
            mock_settings.AI_CACHE_TTL_HOURS = 72
            loaded = _load_cached_analysis("t1", db)
        assert loaded is None

    def test_upsert_overwrites_existing(self, db):
        _wallet(db)
        _trade(db)
        r1 = {"signal": "SPECULATIVE", "risk": "LOW", "price_insight": "x", "behavior": "x", "verdict": "x", "provider": "p1", "model_version": "m1"}
        _save_analysis_to_db("t1", r1, {}, db)
        r2 = {"signal": "CONVICTION", "risk": "HIGH", "price_insight": "y", "behavior": "y", "verdict": "y", "provider": "p2", "model_version": "m2"}
        _save_analysis_to_db("t1", r2, {}, db)

        loaded = _load_cached_analysis("t1", db)
        assert loaded["signal"] == "CONVICTION"
        assert loaded["provider"] == "p2"

    def test_invalidate_removes_row(self, db):
        _wallet(db)
        _trade(db)
        r = {"signal": "CONSENSUS", "risk": "MEDIUM", "price_insight": "x", "behavior": "x", "verdict": "x", "provider": "p", "model_version": "m"}
        _save_analysis_to_db("t1", r, {}, db)

        removed = invalidate_cache("t1", db)
        assert removed is True
        assert _load_cached_analysis("t1", db) is None

    def test_invalidate_nonexistent_returns_false(self, db):
        assert invalidate_cache("ghost", db) is False


# ---------------------------------------------------------------------------
# DB cache â€” wallet summary
# ---------------------------------------------------------------------------

class TestWalletSummaryCache:
    def test_load_returns_none_when_no_summary(self, db):
        w = _wallet(db)
        assert _load_cached_wallet_summary(w) is None

    def test_save_and_load_roundtrip(self, db):
        _wallet(db)
        _save_wallet_summary(_ADDR, "Trades concentrated in politics markets with strong YES bias.", db)
        w = db.query(Wallet).filter(Wallet.address == _ADDR).first()
        result = _load_cached_wallet_summary(w)
        assert result == "Trades concentrated in politics markets with strong YES bias."

    def test_expired_summary_returns_none(self, db):
        _wallet(db)
        _save_wallet_summary(_ADDR, "Old summary.", db)
        w = db.query(Wallet).filter(Wallet.address == _ADDR).first()
        w.ai_summary_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=48)
        db.commit()
        w = db.query(Wallet).filter(Wallet.address == _ADDR).first()
        assert _load_cached_wallet_summary(w) is None

    def test_invalidate_clears_summary(self, db):
        _wallet(db)
        _save_wallet_summary(_ADDR, "Some summary.", db)
        removed = invalidate_wallet_summary(_ADDR, db)
        assert removed is True
        w = db.query(Wallet).filter(Wallet.address == _ADDR).first()
        assert w.ai_summary is None

    def test_invalidate_nonexistent_returns_false(self, db):
        assert invalidate_wallet_summary("0xdeadbeef", db) is False


# ---------------------------------------------------------------------------
# build_trade_context
# ---------------------------------------------------------------------------

class TestBuildTradeContext:
    def test_returns_expected_keys(self, db):
        _wallet(db)
        trade = _trade(db, price=0.65, size=100.0)
        ctx = build_trade_context(trade, db)
        for key in (
            "trade_value", "market_yes_pct", "market_sentiment", "market_total_trades",
            "wallet_total_trades", "wallet_bias", "is_first_trade_on_market",
            "size_vs_wallet_avg", "price_vs_consensus",
        ):
            assert key in ctx, f"Missing key: {key}"

    def test_first_trade_on_market_detected(self, db):
        _wallet(db)
        trade = _trade(db, trade_id="first", condition_id="cond-new", price=0.5, size=10.0)
        ctx = build_trade_context(trade, db)
        assert ctx["is_first_trade_on_market"] is True

    def test_trade_value_is_price_times_size(self, db):
        _wallet(db)
        trade = _trade(db, price=0.4, size=50.0)
        ctx = build_trade_context(trade, db)
        assert abs(ctx["trade_value"] - 20.0) < 0.01

    def test_market_yes_pct_counts_correctly(self, db):
        _wallet(db)
        _trade(db, trade_id="y1", side="YES", price=0.6, size=10.0)
        _trade(db, trade_id="y2", side="YES", price=0.6, size=10.0)
        trade = _trade(db, trade_id="n1", side="NO", price=0.4, size=10.0)
        ctx = build_trade_context(trade, db)
        # 2 YES out of 3 = 67%
        assert ctx["market_yes_pct"] == 67

    def test_size_vs_wallet_avg_reflects_position_scale(self, db):
        _wallet(db)
        # small average trade
        _trade(db, trade_id="s1", price=0.5, size=10.0, hours_ago=2)
        _trade(db, trade_id="s2", price=0.5, size=10.0, hours_ago=1)
        # big trade now
        big_trade = _trade(db, trade_id="big", price=0.5, size=100.0)
        ctx = build_trade_context(big_trade, db)
        assert ctx["size_vs_wallet_avg"] > 1.0


# ---------------------------------------------------------------------------
# analyze_trade â€” mock provider
# ---------------------------------------------------------------------------

class TestAnalyzeTrade:
    def test_returns_none_when_no_provider(self, db):
        _wallet(db)
        trade = _trade(db)
        with patch("app.ai_analysis._detect_provider_candidates", return_value=[]):
            result = analyze_trade(trade, db)
        assert result is None

    def test_uses_local_model_analysis_when_no_provider_but_trade_is_scored(self, db):
        _wallet(db)
        trade = _trade(db, notable_score=0.84)
        with patch("app.ai_analysis._detect_provider_candidates", return_value=[]), \
             patch("app.ai_analysis.get_signal_model", return_value=None), \
             patch("app.ai_analysis.effective_signal_threshold", return_value=0.7):
            result = analyze_trade(trade, db)

        assert result is not None
        assert result["provider"] == "Local signal model"
        assert result["signal"] == "CONVICTION"
        assert result["risk"] == "MEDIUM"
        assert result["context"]["model_signal_score"] == 0.84

        row = db.query(TradeAnalysis).filter(TradeAnalysis.trade_id == "t1").first()
        assert row is not None
        assert row.provider == "Local signal model"

    def test_returns_cached_result_on_second_call(self, db):
        _wallet(db)
        trade = _trade(db)
        mock_result = {
            "signal": "CONVICTION", "risk": "HIGH",
            "price_insight": "x", "behavior": "x", "verdict": "x",
            "provider": "Claude (claude-sonnet-4-6)", "model_version": "claude-sonnet-4-6",
        }
        with patch("app.ai_analysis._detect_provider_candidates", return_value=[("anthropic", "claude-sonnet-4-6")]):
            with patch("app.ai_analysis._analyze_with_anthropic", return_value=mock_result):
                first = analyze_trade(trade, db)

        # Second call should hit DB cache without calling the provider
        with patch("app.ai_analysis._analyze_with_anthropic") as mock_api:
            second = analyze_trade(trade, db)
            mock_api.assert_not_called()

        assert second is not None
        assert second["_from_cache"] is True

    def test_falls_back_to_ollama_when_anthropic_fails(self, db):
        _wallet(db)
        trade = _trade(db)
        ollama_result = {
            "signal": "SPECULATIVE", "risk": "MEDIUM",
            "price_insight": "x", "behavior": "x", "verdict": "x",
            "provider": "Ollama (mistral)", "model_version": "mistral",
        }
        with patch("app.ai_analysis._detect_provider_candidates", return_value=[("anthropic", "claude-sonnet-4-6"), ("ollama", "mistral")]):
            with patch("app.ai_analysis._analyze_with_anthropic", return_value=None):
                with patch("app.ai_analysis._analyze_with_ollama", return_value=ollama_result):
                    result = analyze_trade(trade, db)
        assert result is not None
        assert result["provider"] == "Ollama (mistral)"

    def test_saves_result_to_db(self, db):
        _wallet(db)
        trade = _trade(db)
        mock_result = {
            "signal": "CONSENSUS", "risk": "LOW",
            "price_insight": "x", "behavior": "x", "verdict": "x",
            "provider": "Claude (claude-sonnet-4-6)", "model_version": "claude-sonnet-4-6",
        }
        with patch("app.ai_analysis._detect_provider_candidates", return_value=[("anthropic", "claude-sonnet-4-6")]):
            with patch("app.ai_analysis._analyze_with_anthropic", return_value=mock_result):
                analyze_trade(trade, db)

        row = db.query(TradeAnalysis).filter(TradeAnalysis.trade_id == "t1").first()
        assert row is not None
        assert row.signal == "CONSENSUS"


# ---------------------------------------------------------------------------
# get_trade_summary â€” mock provider
# ---------------------------------------------------------------------------

class TestGetTradeSummary:
    def test_returns_none_for_empty_trades(self, db):
        assert get_trade_summary([], db) is None

    def test_returns_none_when_no_provider(self, db):
        _wallet(db)
        trades = [_trade(db)]
        with patch("app.ai_analysis._detect_provider_candidates", return_value=[]):
            result = get_trade_summary(trades, db)
        assert result is None

    def test_returns_cached_summary_without_calling_provider(self, db):
        _wallet(db)
        trades = [_trade(db)]
        _save_wallet_summary(_ADDR, "Cached wallet insight.", db)

        with patch("app.ai_analysis._detect_provider_candidates") as mock_detect:
            result = get_trade_summary(trades, db)
            mock_detect.assert_not_called()

        assert result == "Cached wallet insight."

    def test_saves_summary_to_wallet_row(self, db):
        _wallet(db)
        trades = [_trade(db)]
        with patch("app.ai_analysis._detect_provider_candidates", return_value=[("anthropic", "claude-sonnet-4-6")]):
            with patch("app.ai_analysis._summarize_with_anthropic", return_value="Strong YES bias on politics."):
                result = get_trade_summary(trades, db)

        assert result == "Strong YES bias on politics."
        w = db.query(Wallet).filter(Wallet.address == _ADDR).first()
        assert w.ai_summary == "Strong YES bias on politics."
        assert w.ai_summary_at is not None
