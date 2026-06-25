# Summary: Tests trade downloading and saving.
# Details: It checks this part of the project so future code changes do not silently break expected behavior.
"""Tests for app/ingest.py â€” normalize, deduplicate, and insert pipeline."""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ingest import (
    cleanup_duplicate_trades,
    fetch_trades_for_wallet,
    find_duplicate_groups,
    normalize_trade,
    refresh_wallet,
)
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

_RAW_TRADE = {
    "id": "external-id-001",
    "conditionId": "cond-1",
    "title": "Test Market",
    "side": "BUY",
    "price": 0.55,
    "size": 10.0,
    "timestamp": 1713139200,
}


class TestNormalizeTrade:
    def test_buy_maps_to_yes(self):
        result = normalize_trade({**_RAW_TRADE, "side": "BUY"}, _ADDR)
        assert result is not None
        assert result["side"] == "YES"

    def test_sell_maps_to_no(self):
        result = normalize_trade({**_RAW_TRADE, "side": "SELL"}, _ADDR)
        assert result is not None
        assert result["side"] == "NO"

    def test_outcome_yes_fallback(self):
        raw = {**_RAW_TRADE}
        del raw["side"]
        result = normalize_trade({**raw, "outcome": "YES"}, _ADDR)
        assert result is not None
        assert result["side"] == "YES"

    def test_prefers_external_id_over_hash(self):
        result = normalize_trade({**_RAW_TRADE, "id": "my-trade-id"}, _ADDR)
        assert result["id"] == "my-trade-id"

    def test_generates_hash_id_when_no_external_id(self):
        raw = {k: v for k, v in _RAW_TRADE.items() if k != "id"}
        raw["transactionHash"] = "0xhash"
        raw["asset"] = "asset-1"
        result = normalize_trade(raw, _ADDR)
        assert result is not None
        assert len(result["id"]) == 24

    def test_returns_none_for_zero_price(self):
        result = normalize_trade({**_RAW_TRADE, "price": 0.0}, _ADDR)
        assert result is None

    def test_returns_none_for_zero_size(self):
        result = normalize_trade({**_RAW_TRADE, "size": 0.0}, _ADDR)
        assert result is None

    def test_returns_none_for_missing_condition_id(self):
        raw = {k: v for k, v in _RAW_TRADE.items() if k != "conditionId"}
        result = normalize_trade(raw, _ADDR)
        assert result is None

    def test_returns_none_for_unknown_side(self):
        result = normalize_trade({**_RAW_TRADE, "side": "UNKNOWN", "outcome": "MAYBE"}, _ADDR)
        assert result is None

    def test_parses_iso_timestamp(self):
        result = normalize_trade({**_RAW_TRADE, "timestamp": "2024-04-15T12:30:00Z"}, _ADDR)
        assert result is not None
        assert result["traded_at"].year == 2024
        assert result["traded_at"].month == 4

    def test_handles_malformed_payload_gracefully(self):
        result = normalize_trade({"invalid": "data", "price": "not-a-number"}, _ADDR)
        assert result is None


class TestRefreshWalletPipeline:
    def _make_wallet(self, db):
        w = Wallet(address=_ADDR)
        db.add(w)
        db.commit()
        db.refresh(w)
        return w

    def test_inserts_new_trades(self, db):
        wallet = self._make_wallet(db)
        with patch("app.ingest.fetch_trades_for_wallet", return_value=[_RAW_TRADE]):
            result = refresh_wallet(db, wallet)
        assert result["inserted"] == 1
        assert result["status"] == "success"
        assert db.query(Trade).filter(Trade.wallet_address == _ADDR).count() == 1

    def test_does_not_insert_duplicate_trade_id(self, db):
        wallet = self._make_wallet(db)
        with patch("app.ingest.fetch_trades_for_wallet", return_value=[_RAW_TRADE]):
            first = refresh_wallet(db, wallet)
            second = refresh_wallet(db, wallet)
        assert first["inserted"] == 1
        assert second["inserted"] == 0
        assert db.query(Trade).count() == 1

    def test_deduplicates_repeated_trade_ids_inside_one_api_response(self, db):
        wallet = self._make_wallet(db)
        with patch("app.ingest.fetch_trades_for_wallet", return_value=[_RAW_TRADE, _RAW_TRADE]):
            result = refresh_wallet(db, wallet)
        assert result["inserted"] == 1
        assert result["duplicates"] == 1
        assert db.query(Trade).count() == 1

    def test_no_new_status_when_no_new_trades(self, db):
        wallet = self._make_wallet(db)
        with patch("app.ingest.fetch_trades_for_wallet", return_value=[_RAW_TRADE]):
            refresh_wallet(db, wallet)
        with patch("app.ingest.fetch_trades_for_wallet", return_value=[_RAW_TRADE]):
            result = refresh_wallet(db, wallet)
        assert result["status"] == "no_new"

    def test_creates_sync_event_on_success(self, db):
        wallet = self._make_wallet(db)
        with patch("app.ingest.fetch_trades_for_wallet", return_value=[_RAW_TRADE]):
            refresh_wallet(db, wallet)
        events = db.query(SyncEvent).filter(SyncEvent.wallet_address == _ADDR).all()
        assert len(events) == 1
        assert events[0].status == "success"
        assert events[0].inserted_count == 1

    def test_creates_error_sync_event_on_failure(self, db):
        wallet = self._make_wallet(db)
        with patch("app.ingest.fetch_trades_for_wallet", side_effect=ConnectionError("timeout")):
            result = refresh_wallet(db, wallet)
        assert result["status"] == "error"
        assert "timeout" in result["error"]
        events = db.query(SyncEvent).filter(SyncEvent.wallet_address == _ADDR).all()
        assert events[0].status == "error"

    def test_handles_malformed_api_response_gracefully(self, db):
        wallet = self._make_wallet(db)
        bad_trades = [{"invalid": "garbage", "price": "not-a-number"}]
        with patch("app.ingest.fetch_trades_for_wallet", return_value=bad_trades):
            result = refresh_wallet(db, wallet)
        assert result["status"] == "no_new"
        assert result["inserted"] == 0

    def test_updates_wallet_last_refresh_status(self, db):
        wallet = self._make_wallet(db)
        with patch("app.ingest.fetch_trades_for_wallet", return_value=[_RAW_TRADE]):
            refresh_wallet(db, wallet)
        db.refresh(wallet)
        assert wallet.last_refresh_status == "success"
        assert wallet.last_checked_at is not None

    def test_error_sets_wallet_error_fields(self, db):
        wallet = self._make_wallet(db)
        with patch("app.ingest.fetch_trades_for_wallet", side_effect=Exception("API down")):
            refresh_wallet(db, wallet)
        db.refresh(wallet)
        assert wallet.last_refresh_status == "error"
        assert "API down" in (wallet.last_error_message or "")


class TestRefreshWalletScoring:
    def _make_wallet(self, db):
        w = Wallet(address=_ADDR)
        db.add(w)
        db.commit()
        db.refresh(w)
        return w

    def test_scoring_failure_does_not_break_refresh(self, db):
        wallet = self._make_wallet(db)
        with patch("app.ingest.fetch_trades_for_wallet", return_value=[_RAW_TRADE]), \
             patch("app.ml.model.get_signal_model", return_value=object()), \
             patch("app.ml.features.build_features_for_wallet", side_effect=RuntimeError("scoring boom")):
            result = refresh_wallet(db, wallet)
        assert result["status"] == "success"
        assert result["inserted"] == 1
        assert result["error"] is None
        assert db.query(Trade).count() == 1

    def test_refresh_writes_notable_scores_when_model_loaded(self, db):
        import numpy as np

        wallet = self._make_wallet(db)
        raw_trades = [
            {**_RAW_TRADE, "id": f"scored-{i}", "timestamp": _RAW_TRADE["timestamp"] + i * 3600}
            for i in range(12)
        ]

        class _FakeModel:
            # Refresh scores via predict_full (it selects the model's columns
            # from the full feature matrix before predicting).
            def predict_full(self, X):
                return np.full(len(X), 0.42)

        with patch("app.ingest.fetch_trades_for_wallet", return_value=raw_trades), \
             patch("app.ml.model.get_signal_model", return_value=_FakeModel()):
            result = refresh_wallet(db, wallet)

        assert result["inserted"] == 12
        scored = db.query(Trade).filter(Trade.notable_score.isnot(None)).all()
        # Only trades with >= 10 prior trades are scorable: the 11th and 12th.
        assert {t.trade_id for t in scored} == {"scored-10", "scored-11"}
        assert all(t.notable_score == pytest.approx(0.42) for t in scored)

    def test_refresh_skips_scoring_when_model_missing(self, db):
        wallet = self._make_wallet(db)
        with patch("app.ingest.fetch_trades_for_wallet", return_value=[_RAW_TRADE]), \
             patch("app.ml.model.get_signal_model", return_value=None):
            result = refresh_wallet(db, wallet)
        assert result["status"] == "success"
        assert db.query(Trade).filter(Trade.notable_score.isnot(None)).count() == 0


class TestFindDuplicateGroups:
    def test_no_duplicates_returns_empty(self, db):
        w = Wallet(address=_ADDR)
        db.add(w)
        t = Trade(
            wallet_address=_ADDR, trade_id="unique-1", condition_id="c1",
            side="YES", price=0.5, size=10.0, traded_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(t)
        db.commit()
        assert find_duplicate_groups(db) == []

    def test_detects_semantic_duplicates(self, db):
        w = Wallet(address=_ADDR)
        db.add(w)
        ts = datetime(2026, 1, 1, 12, 0)
        db.add_all([
            Trade(wallet_address=_ADDR, trade_id="dup-a", condition_id="c1", side="YES", price=0.5, size=10.0, traded_at=ts),
            Trade(wallet_address=_ADDR, trade_id="dup-b", condition_id="c1", side="YES", price=0.5, size=10.0, traded_at=ts),
        ])
        db.commit()
        groups = find_duplicate_groups(db)
        assert len(groups) == 1
        assert groups[0]["duplicate_count"] == 2


class TestCleanupDuplicateTrades:
    def test_removes_extra_duplicates_keeps_one(self, db):
        w = Wallet(address=_ADDR)
        db.add(w)
        ts = datetime(2026, 1, 1, 12, 0)
        db.add_all([
            Trade(wallet_address=_ADDR, trade_id="del-a", condition_id="c1", side="YES", price=0.5, size=10.0, traded_at=ts),
            Trade(wallet_address=_ADDR, trade_id="del-b", condition_id="c1", side="YES", price=0.5, size=10.0, traded_at=ts),
            Trade(wallet_address=_ADDR, trade_id="del-c", condition_id="c1", side="YES", price=0.5, size=10.0, traded_at=ts),
        ])
        db.commit()
        removed = cleanup_duplicate_trades(db)
        assert removed == 2
        assert db.query(Trade).count() == 1

    def test_no_duplicates_returns_zero(self, db):
        w = Wallet(address=_ADDR)
        db.add(w)
        db.add(Trade(
            wallet_address=_ADDR, trade_id="unique", condition_id="c1",
            side="YES", price=0.5, size=10.0,
            traded_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ))
        db.commit()
        assert cleanup_duplicate_trades(db) == 0


class TestFetchTradesForWallet:
    def test_single_batch_when_fetch_all_false(self):
        with patch("app.ingest._fetch_trade_batch", return_value=[]) as mock_batch:
            fetch_trades_for_wallet("0xabc", limit=50, fetch_all=False)
        mock_batch.assert_called_once_with(address="0xabc", limit=50)

    def test_paginates_until_empty_batch(self):
        call_count = 0

        def mock_batch(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [_RAW_TRADE] * 2  # full batch (limit=2)
            return []

        with patch("app.ingest._fetch_trade_batch", side_effect=mock_batch):
            result = fetch_trades_for_wallet("0xabc", limit=2, fetch_all=True)

        assert call_count == 2
        assert len(result) == 2
