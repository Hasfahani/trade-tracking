# Tests signal score display in the UI.
"""Tests for the Signal Model UI: pills, trade detail card, wallet section, dashboard stat."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import create_app
from app.models import Base, Trade, Wallet

_ADDR = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _build_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    app = create_app(lifespan_context=None, csrf_enabled=False)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), testing_session


def _add_trade(db, trade_id, notable_score=None, hours_ago=1.0):
    traded_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago)
    db.add(
        Trade(
            wallet_address=_ADDR,
            trade_id=trade_id,
            condition_id="cond-1",
            market_title="Signal Test Market",
            side="YES",
            price=0.5,
            size=10.0,
            traded_at=traded_at,
            notable_score=notable_score,
        )
    )


def _seed(session_factory, scores):
    db = session_factory()
    try:
        db.add(Wallet(address=_ADDR))
        for i, score in enumerate(scores):
            _add_trade(db, f"sig-{i}", notable_score=score, hours_ago=1.0 + i)
        db.commit()
    finally:
        db.close()


class TestSignalPills:
    @pytest.mark.parametrize("path", ["/all-trades", f"/wallets/{_ADDR}/trades", "/dashboard"])
    def test_pill_shown_for_high_scores_only(self, path):
        client, session_factory = _build_client()
        _seed(session_factory, [0.84, 0.42, None])

        response = client.get(path)
        assert response.status_code == 200
        assert "Signal 0.84" in response.text
        assert "Signal 0.42" not in response.text
        # Exactly one flagged trade -> exactly one Signal pill
        assert response.text.count("Signal 0.") == 1


class TestTradeDetailCard:
    def test_scored_trade_shows_percentage(self):
        client, session_factory = _build_client()
        _seed(session_factory, [0.84])

        response = client.get("/trades/sig-0")
        assert response.status_code == 200
        assert "Signal Model" in response.text
        assert "84.0%" in response.text
        assert "Trained on this wallet" in response.text

    def test_scored_trade_enables_ai_analysis_without_external_provider(self):
        client, session_factory = _build_client()
        _seed(session_factory, [0.84])

        with patch("app.routes.trades.settings.ai_provider_configured", return_value=False):
            response = client.get("/trades/sig-0")
        assert response.status_code == 200
        assert 'id="ai-btn"' in response.text
        assert "AI not configured" not in response.text

    def test_unscored_trade_shows_insufficient_history(self):
        client, session_factory = _build_client()
        _seed(session_factory, [None])

        response = client.get("/trades/sig-0")
        assert response.status_code == 200
        assert "Not scored" in response.text
        assert "insufficient wallet history" in response.text


class TestWalletDetailSection:
    def test_flagged_count_and_recent_list(self):
        client, session_factory = _build_client()
        _seed(session_factory, [0.95, 0.71, 0.72, 0.73, 0.74, 0.75, 0.99, 0.2, None])

        response = client.get(f"/wallets/{_ADDR}")
        assert response.status_code == 200
        assert "Signal Model" in response.text
        assert "7 flagged trades" in response.text
        # Only the 5 most recent flagged trades are listed (hours_ago grows with index).
        assert response.text.count("Signal 0.") == 5
        assert "Signal 0.95" in response.text  # most recent flagged
        assert "Signal 0.75" not in response.text  # 6th most recent flagged

    def test_degrades_gracefully_when_model_never_loaded(self):
        client, session_factory = _build_client()
        _seed(session_factory, [None, None])

        with patch("app.routes.wallets.get_signal_model", return_value=None):
            response = client.get(f"/wallets/{_ADDR}")
        assert response.status_code == 200
        assert "0 flagged trades" in response.text
        assert "Signal model not available" in response.text


class TestDashboardStat:
    def test_counts_only_recent_flagged_trades(self):
        client, session_factory = _build_client()
        db = session_factory()
        try:
            db.add(Wallet(address=_ADDR))
            _add_trade(db, "fresh-high", notable_score=0.9, hours_ago=2)
            _add_trade(db, "fresh-low", notable_score=0.3, hours_ago=2)
            _add_trade(db, "old-high", notable_score=0.9, hours_ago=48)
            _add_trade(db, "fresh-null", notable_score=None, hours_ago=2)
            db.commit()
        finally:
            db.close()

        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "Model-flagged (24h)" in response.text
        assert 'data-countup="1"' in response.text
