# Summary: Tests model input creation.
# Details: It checks this part of the project so future code changes do not silently break expected behavior.
import math
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.ml.features import (
    FEATURE_COUNT,
    FEATURE_LABELS,
    FEATURE_NAMES,
    MIN_PRIOR_TRADES,
    build_dataset,
    build_features_for_wallet,
)
from app.models import Base, Trade, Wallet

BASE_TS = datetime(2026, 1, 1, 12, 0, 0)


def make_trade(index, *, price=0.5, size=10.0, side="YES", hours_apart=1.0, wallet="0xabc", condition_id="cond-1"):
    return SimpleNamespace(
        trade_id=f"{wallet}-trade-{index}",
        wallet_address=wallet,
        condition_id=condition_id,
        side=side,
        price=price,
        size=size,
        traded_at=BASE_TS + timedelta(hours=index * hours_apart),
    )


def make_trades(count, **kwargs):
    return [make_trade(i, **kwargs) for i in range(count)]


def test_fewer_than_min_prior_trades_yields_no_rows():
    X, y, trade_ids = build_features_for_wallet(make_trades(MIN_PRIOR_TRADES))

    assert X.shape == (0, FEATURE_COUNT)
    assert y.shape == (0,)
    assert trade_ids == []


def test_first_qualifying_trade_is_the_eleventh():
    X, y, trade_ids = build_features_for_wallet(make_trades(MIN_PRIOR_TRADES + 1))

    assert X.shape == (1, FEATURE_COUNT)
    assert y.shape == (1,)
    assert trade_ids == [f"0xabc-trade-{MIN_PRIOR_TRADES}"]


def test_feature_names_and_labels_are_consistent():
    assert len(FEATURE_NAMES) == FEATURE_COUNT
    assert set(FEATURE_LABELS) == set(FEATURE_NAMES)


def test_feature_values_match_hand_computation():
    trades = make_trades(MIN_PRIOR_TRADES)  # price 0.5, size 10 -> value 5, 1h apart
    trades.append(make_trade(MIN_PRIOR_TRADES, price=0.7, size=10.0, side="NO"))

    X, y, trade_ids = build_features_for_wallet(trades)

    assert trade_ids == [f"0xabc-trade-{MIN_PRIOR_TRADES}"]
    row = X[0]
    assert row[0] == 0.0  # side NO
    assert row[1] == 0.7
    assert math.isclose(row[2], 0.2)  # 0.7 - prior mean price 0.5
    assert math.isclose(row[3], math.log1p(5.0))  # prior mean value 5
    assert row[4] == 1.0  # one hour since previous trade
    assert row[5] == float(MIN_PRIOR_TRADES)  # all 10 prior trades within 24h
    assert math.isclose(row[6], 22 / 23.0)  # 12:00 + 10h = 22:00 UTC
    assert row[7] == 0.0  # uniform prior values -> zero coefficient of variation
    assert row[8] == 0.0  # no prior trade ever fired the notable rule
    assert row[9] == 0.0  # 10 prior trades on this market -> not a first entry
    assert row[10] == 1.0  # all prior trades on this market
    assert math.isclose(row[11], 0.4)  # |0.7 - 0.5| * 2
    assert math.isclose(row[12], math.log1p(7.0))
    assert math.isclose(row[13], math.log(7.0 / 5.0))
    assert math.isclose(row[14], 7.0 / 5.0)  # std-zero fallback
    # value 7 vs std-zero fallback threshold 2 * 5 = 10 -> not notable
    assert y[0] == 0.0


def test_market_features_flag_first_entry_on_new_market():
    trades = make_trades(MIN_PRIOR_TRADES)  # all on cond-1
    trades.append(make_trade(MIN_PRIOR_TRADES, condition_id="cond-2"))

    X, _, _ = build_features_for_wallet(trades)

    row = X[0]
    assert row[9] == 1.0  # first trade on cond-2
    assert row[10] == 0.0  # zero of the 10 prior trades were on cond-2


def test_prior_notable_rate_and_cv_reflect_past_outliers():
    trades = make_trades(MIN_PRIOR_TRADES)  # uniform value 5
    trades.append(make_trade(MIN_PRIOR_TRADES, size=1000.0))  # value 500 -> notable at its time
    trades.append(make_trade(MIN_PRIOR_TRADES + 1))  # back to value 5

    X, y, _ = build_features_for_wallet(trades)

    assert y[0] == 1.0  # the 500-value trade is notable vs uniform history
    # Second row: trades 1..10 were rule-eligible priors and exactly one of
    # them (the 500-value trade) fired the rule.
    assert X[1][8] == pytest.approx(1.0 / 10.0)
    # Prior values are ten 5s and one 500: mean 50, population std ~142.3.
    assert X[1][7] == pytest.approx(math.sqrt(20250.0) / 50.0)
    assert y[1] == 0.0


def test_expanding_window_earlier_rows_unaffected_by_later_trades():
    trades = [
        make_trade(i, price=0.3 + 0.02 * i, size=10.0 + 3.0 * i, side="YES" if i % 2 else "NO")
        for i in range(MIN_PRIOR_TRADES + 4)
    ]
    X_before, y_before, ids_before = build_features_for_wallet(trades)

    later = [
        make_trade(i, price=0.9, size=500.0)
        for i in range(len(trades), len(trades) + 5)
    ]
    X_after, y_after, ids_after = build_features_for_wallet(trades + later)

    assert ids_after[: len(ids_before)] == ids_before
    np.testing.assert_array_equal(X_after[: len(ids_before)], X_before)
    np.testing.assert_array_equal(y_after[: len(ids_before)], y_before)


def test_changing_own_size_changes_observed_value_features_and_label():
    prior = make_trades(MIN_PRIOR_TRADES)  # uniform value 5 -> prior std 0
    small = prior + [make_trade(MIN_PRIOR_TRADES, size=10.0)]
    large = prior + [make_trade(MIN_PRIOR_TRADES, size=1000.0)]

    X_small, y_small, ids_small = build_features_for_wallet(small)
    X_large, y_large, ids_large = build_features_for_wallet(large)

    assert ids_small == ids_large
    np.testing.assert_array_equal(X_small[:, :12], X_large[:, :12])
    assert X_large[0, 12] > X_small[0, 12]
    assert X_large[0, 13] > X_small[0, 13]
    assert X_large[0, 14] > X_small[0, 14]
    assert y_small[0] == 0.0  # value 5 <= 2 * prior mean 5
    assert y_large[0] == 1.0  # value 500 > 2 * prior mean 5


def test_label_uses_mean_plus_two_std_when_std_nonzero():
    # Alternating values 4 and 6: mean 5, population std 1 -> threshold 7.
    prior = [
        make_trade(i, price=0.5, size=8.0 if i % 2 == 0 else 12.0)
        for i in range(MIN_PRIOR_TRADES)
    ]
    below = prior + [make_trade(MIN_PRIOR_TRADES, price=0.5, size=13.8)]  # value 6.9
    above = prior + [make_trade(MIN_PRIOR_TRADES, price=0.5, size=14.2)]  # value 7.1

    _, y_below, _ = build_features_for_wallet(below)
    _, y_above, _ = build_features_for_wallet(above)

    assert y_below[0] == 0.0
    assert y_above[0] == 1.0


def test_build_dataset_groups_by_wallet_and_returns_timestamps():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        for wallet in ("0xaaa", "0xbbb"):
            session.add(Wallet(address=wallet))
            for i in range(MIN_PRIOR_TRADES + 2):
                fake = make_trade(i, wallet=wallet)
                session.add(
                    Trade(
                        wallet_address=wallet,
                        trade_id=fake.trade_id,
                        condition_id="cond-1",
                        side=fake.side,
                        price=fake.price,
                        size=fake.size,
                        traded_at=fake.traded_at,
                    )
                )
        # Wallet with too few trades contributes nothing.
        session.add(Wallet(address="0xccc"))
        session.add(
            Trade(
                wallet_address="0xccc",
                trade_id="0xccc-trade-0",
                condition_id="cond-1",
                side="YES",
                price=0.5,
                size=10.0,
                traded_at=BASE_TS,
            )
        )
        session.commit()

        X, y, trade_ids, timestamps = build_dataset(session)
    finally:
        session.close()

    assert X.shape == (4, FEATURE_COUNT)  # 2 qualifying trades per eligible wallet
    assert y.shape == (4,)
    assert len(trade_ids) == 4
    assert len(timestamps) == 4
    assert set(trade_ids) == {
        f"0xaaa-trade-{MIN_PRIOR_TRADES}",
        f"0xaaa-trade-{MIN_PRIOR_TRADES + 1}",
        f"0xbbb-trade-{MIN_PRIOR_TRADES}",
        f"0xbbb-trade-{MIN_PRIOR_TRADES + 1}",
    }
    for trade_id, traded_at in zip(trade_ids, timestamps):
        index = int(trade_id.rsplit("-", 1)[1])
        assert traded_at == BASE_TS + timedelta(hours=index)
