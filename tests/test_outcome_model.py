# Summary: Tests the resolved-market outcome (profitability) model end to end.
# Details: It checks this part of the project so future code changes do not silently break expected behavior.
"""Tests for the outcome model: label logic, leak-safe dataset, numpy inference, training."""
import json
import math
from datetime import datetime, timedelta

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ml.outcome_features import (
    FEATURE_COUNT,
    FEATURE_NAMES,
    NEUTRAL_WIN_RATE,
    build_outcome_dataset,
    trade_won,
)
from app.ml.outcome_model import OutcomeModel, load_outcome_model
from app.models import Base, MarketResolution, Trade, Wallet

T0 = datetime(2026, 1, 1, 12, 0, 0)
WALLET = "0x" + "1" * 40


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _add_trade(db, tid, condition_id, side, token, price, size, hours):
    db.add(Trade(
        trade_id=tid, wallet_address=WALLET, condition_id=condition_id,
        market_title=condition_id, side=side, outcome_token=token,
        price=price, size=size, traded_at=T0 + timedelta(hours=hours),
    ))


def _resolve(db, condition_id, outcome, hours):
    db.add(MarketResolution(
        condition_id=condition_id, outcome=outcome, resolved_at=T0 + timedelta(hours=hours)
    ))


class TestTradeWon:
    def test_buy_winning_token(self):
        assert trade_won("YES", "YES", "YES") == 1.0  # bought token that won

    def test_buy_losing_token(self):
        assert trade_won("YES", "NO", "YES") == 0.0   # bought NO, market YES

    def test_sell_winning_token_loses(self):
        assert trade_won("NO", "YES", "YES") == 0.0   # sold token that won

    def test_sell_losing_token_wins(self):
        assert trade_won("NO", "YES", "NO") == 1.0    # sold token that lost

    def test_undecidable_when_unresolved_or_no_token(self):
        assert trade_won("YES", None, "YES") is None
        assert trade_won("YES", "YES", "UNRESOLVED") is None


class TestDataset:
    def test_label_and_leak_safe_win_rate(self):
        db = _session()
        db.add(Wallet(address=WALLET))
        # m1: resolves early (T0+1h), a win.
        _add_trade(db, "t1", "m1", "YES", "YES", 0.4, 10, hours=0)
        _resolve(db, "m1", "YES", hours=1)
        # m2: resolves late (T0+10h), a loss (bought YES, resolved NO).
        _add_trade(db, "t2", "m2", "YES", "YES", 0.6, 10, hours=2)
        _resolve(db, "m2", "NO", hours=10)
        # m3: the row under inspection, traded T0+5h, a win.
        _add_trade(db, "t3", "m3", "YES", "YES", 0.5, 10, hours=5)
        _resolve(db, "m3", "YES", hours=6)
        db.commit()

        X, y, ids, ts = build_outcome_dataset(db)

        assert X.shape == (3, FEATURE_COUNT)
        order = {tid: i for i, tid in enumerate(ids)}
        # labels: t1 win, t2 loss, t3 win
        assert y[order["t1"]] == 1.0
        assert y[order["t2"]] == 0.0
        assert y[order["t3"]] == 1.0

        wr = FEATURE_NAMES.index("prior_resolved_win_rate")
        cnt = FEATURE_NAMES.index("log1p_prior_resolved_count")
        # t1: no prior resolved history -> neutral.
        assert X[order["t1"]][wr] == pytest.approx(NEUTRAL_WIN_RATE)
        assert X[order["t1"]][cnt] == pytest.approx(0.0)
        # t3 at T0+5h: only m1 (resolved T0+1h) is known; m2 resolves later and
        # must NOT leak. So win_rate = 1/1 = 1.0 over exactly one known result.
        assert X[order["t3"]][wr] == pytest.approx(1.0)
        assert X[order["t3"]][cnt] == pytest.approx(math.log1p(1))

    def test_market_implied_win_prob_flips_with_side(self):
        # The market-implied win prob is `price` for a BUY and `1-price` for a
        # SELL: a 0.7 BUY and a 0.3 SELL both imply a 0.7 chance of winning.
        db = _session()
        db.add(Wallet(address=WALLET))
        _add_trade(db, "buy", "m1", "YES", "YES", 0.7, 10, hours=0)
        _resolve(db, "m1", "YES", hours=1)
        _add_trade(db, "sell", "m2", "NO", "YES", 0.3, 10, hours=2)
        _resolve(db, "m2", "NO", hours=3)
        db.commit()

        X, y, ids, ts = build_outcome_dataset(db)
        idx = FEATURE_NAMES.index("market_implied_win_prob")
        assert X[ids.index("buy")][idx] == pytest.approx(0.7)
        assert X[ids.index("sell")][idx] == pytest.approx(0.7)

    def test_unresolved_trades_are_not_rows(self):
        db = _session()
        db.add(Wallet(address=WALLET))
        _add_trade(db, "t1", "m1", "YES", "YES", 0.5, 10, hours=0)  # no resolution
        db.commit()
        X, y, ids, ts = build_outcome_dataset(db)
        assert ids == [] and X.shape == (0, FEATURE_COUNT)

    def test_market_context_counts_cross_wallet_prior_activity(self):
        db = _session()
        other = "0x" + "9" * 40
        db.add(Wallet(address=WALLET))
        db.add(Wallet(address=other))
        # Another wallet trades the market first, then our wallet does.
        db.add(Trade(trade_id="a", wallet_address=other, condition_id="m1", side="YES",
                     outcome_token="YES", price=0.5, size=10, traded_at=T0))
        _add_trade(db, "t1", "m1", "YES", "YES", 0.5, 10, hours=1)
        _resolve(db, "m1", "YES", hours=5)
        db.commit()

        X, y, ids, ts = build_outcome_dataset(db)
        row = X[ids.index("t1")]
        # One prior trade on the market from one distinct other wallet.
        assert row[FEATURE_NAMES.index("log1p_market_prior_trades")] == pytest.approx(math.log1p(1))
        assert row[FEATURE_NAMES.index("log1p_distinct_wallets_on_market")] == pytest.approx(math.log1p(1))


class TestInference:
    def test_save_load_predict_roundtrip(self, tmp_path):
        means = [0.0] * FEATURE_COUNT
        stds = [1.0] * FEATURE_COUNT
        w = [0.0] * FEATURE_COUNT
        payload = {
            "w": w, "b": 0.0, "feature_means": means, "feature_stds": stds,
            "feature_names": list(FEATURE_NAMES), "threshold": 0.5,
        }
        path = tmp_path / "outcome.json"
        path.write_text(json.dumps(payload))
        model = load_outcome_model(path)
        assert model is not None
        # all-zero weights -> sigmoid(0) = 0.5 for any input
        score = model.predict(np.zeros((1, FEATURE_COUNT)))
        assert score[0] == pytest.approx(0.5)
        # explanation returns one pair per feature
        assert len(model.explain(np.zeros(FEATURE_COUNT))) == FEATURE_COUNT

    def test_load_missing_returns_none(self, tmp_path):
        assert load_outcome_model(tmp_path / "nope.json") is None

    def test_predict_separates_classes(self):
        # A single informative feature drives the score in opposite directions.
        model = OutcomeModel(
            w=[2.0] + [0.0] * (FEATURE_COUNT - 1),
            b=0.0,
            feature_means=[0.0] * FEATURE_COUNT,
            feature_stds=[1.0] * FEATURE_COUNT,
        )
        hi = np.zeros(FEATURE_COUNT); hi[0] = 3.0
        lo = np.zeros(FEATURE_COUNT); lo[0] = -3.0
        assert model.predict(hi.reshape(1, -1))[0] > 0.9
        assert model.predict(lo.reshape(1, -1))[0] < 0.1


class TestTraining:
    def test_trains_on_separable_synthetic_data(self, tmp_path, monkeypatch):
        # Build a synthetic resolved dataset where price strongly predicts wins:
        # high-price BUYs win, low-price BUYs lose. The model should learn it.
        db = _session()
        db.add(Wallet(address=WALLET))
        for i in range(120):
            wins = i % 2 == 0
            price = 0.8 if wins else 0.2
            cond = f"m{i}"
            _add_trade(db, f"t{i}", cond, "YES", "YES", price, 10, hours=i)
            _resolve(db, cond, "YES" if wins else "NO", hours=i + 0.5)
        db.commit()

        import app.ml.outcome_train as ot
        from contextlib import contextmanager

        @contextmanager
        def fake_ctx():
            yield db
        monkeypatch.setattr(ot, "get_db_context", fake_ctx, raising=False)
        # get_db_context is imported inside the function; patch the source too.
        monkeypatch.setattr("app.db.get_db_context", fake_ctx, raising=False)

        out = tmp_path / "outcome_model_weights.json"
        result = ot.train_outcome_and_export(epochs=300, output_path=out)

        assert result["n_train"] > 0 and result["n_test"] > 0
        # Price is genuinely predictive here -> test ROC-AUC should beat coin flip.
        assert result["test_metrics"]["roc_auc"] > 0.8
        assert "market_implied_win_prob" in result["baseline_metrics"]
        assert "roc_auc_delta" in result["model_vs_market"]
        assert out.exists()
