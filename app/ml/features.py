# Creates model inputs from wallet trade history.
"""Feature engineering for per-wallet observed-trade notability.

Features for each trade are computed from an expanding window of the
wallet's PRIOR trades, plus three observed-value features from the current
trade. This is an anomaly detector, not a forecast: when a trade arrives its
price and size are already public, and the product needs to answer how unusual
that observed trade is relative to the wallet's history.
"""

import math
from collections import defaultdict
from datetime import timedelta
from typing import List, Sequence, Tuple

import numpy as np

from app.models import Trade

MIN_PRIOR_TRADES = 10
MAX_GAP_HOURS = 168.0
MAX_VALUE_CV = 5.0
FEATURE_COUNT = 15

# Order must match the rows built in build_features_for_wallet.
FEATURE_NAMES = [
    "side_yes",
    "price",
    "price_minus_prior_mean_price",
    "log1p_prior_mean_value",
    "hours_since_prev_trade_capped_168",
    "prior_trades_in_24h",
    "hour_of_day_utc_norm",
    "prior_value_cv_capped",
    "prior_notable_rate",
    "is_first_trade_on_market",
    "market_concentration",
    "price_extremity",
    "log1p_trade_value",
    "log_value_vs_prior_mean",
    "value_zscore_capped",
]

# Features that carry the CURRENT trade's observed value (price * size). The
# training label (build_features_for_wallet, below) is a deterministic 2-sigma
# threshold on the current value relative to the wallet's prior history, so
# these features leak the label and must be excluded from an honest model:
#   - value_zscore_capped  IS the label boundary: y == (value_zscore > 2.0)
#   - log_value_vs_prior_mean and log1p_trade_value both carry `value` directly
# See data/model_weights.json history: a model trained WITH these scores a
# perfect ROC-AUC == 1.0, which is leakage, not skill.
LABEL_FEATURE = "value_zscore_capped"  # the label is exactly (this > 2.0)
LEAKAGE_FEATURE_NAMES = (
    "log1p_trade_value",
    "log_value_vs_prior_mean",
    "value_zscore_capped",
)

# The honest, leakage-free feature set: every behavioral/contextual feature,
# none of which carries the current trade's value or size. Trained models record
# the exact set they used in their weights file ("feature_names").
SAFE_FEATURE_NAMES = [name for name in FEATURE_NAMES if name not in LEAKAGE_FEATURE_NAMES]

# Default feature set for training/scoring. The deployed model is leakage-safe.
DEFAULT_FEATURE_NAMES = SAFE_FEATURE_NAMES


def feature_indices(feature_names: Sequence[str]) -> List[int]:
    """Column indices into the full FEATURE_NAMES matrix for the given names.

    Raises KeyError if any name is not a known feature.
    """
    lookup = {name: i for i, name in enumerate(FEATURE_NAMES)}
    try:
        return [lookup[name] for name in feature_names]
    except KeyError as exc:  # pragma: no cover - guards against typos/drift
        raise KeyError(f"Unknown feature name: {exc.args[0]!r}") from exc


def select_feature_columns(X: np.ndarray, feature_names: Sequence[str]) -> np.ndarray:
    """Subset a full (n, FEATURE_COUNT) matrix to the named columns, in order.

    Returns X unchanged when feature_names is empty or already the full set, so
    legacy full-width models keep working without a copy.
    """
    if not feature_names or list(feature_names) == FEATURE_NAMES:
        return X
    return X[:, feature_indices(feature_names)]


# Human-readable labels for the explanation layer ("why" in the UI).
FEATURE_LABELS = {
    "side_yes": "YES/NO side",
    "price": "entry price",
    "price_minus_prior_mean_price": "price vs wallet's usual entry price",
    "log1p_prior_mean_value": "wallet's typical trade value",
    "hours_since_prev_trade_capped_168": "time gap since the wallet's previous trade",
    "prior_trades_in_24h": "wallet's trading pace in the last 24h",
    "hour_of_day_utc_norm": "time of day",
    "prior_value_cv_capped": "how erratic the wallet's trade sizes have been",
    "prior_notable_rate": "wallet's history of outsized trades",
    "is_first_trade_on_market": "first entry on this market",
    "market_concentration": "wallet's focus on this market",
    "price_extremity": "how extreme the entry price is (longshot/near-certain)",
    "log1p_trade_value": "observed trade value",
    "log_value_vs_prior_mean": "trade value vs the wallet's prior average",
    "value_zscore_capped": "trade value deviation from the wallet's prior history",
}


def build_features_for_wallet(trades: Sequence) -> Tuple[np.ndarray, np.ndarray, List[str]]:
    """Build an observed-trade anomaly feature matrix for one wallet's trades.

    Args:
        trades: Trade rows for a single wallet, sorted by traded_at ascending.

    Returns:
        X: float array of shape (n, FEATURE_COUNT).
        y: float array of shape (n,) - 1.0 if the trade's value is notably
           large versus the wallet's prior history, else 0.0.
        trade_ids: trade_id strings aligned with the rows of X and y.

    Trades with fewer than MIN_PRIOR_TRADES prior trades are skipped.
    """
    trades = sorted(trades, key=lambda trade: trade.traded_at)

    rows: List[List[float]] = []
    labels: List[float] = []
    trade_ids: List[str] = []

    price_sum = 0.0
    value_sum = 0.0
    value_sumsq = 0.0
    window_start = 0  # index of the oldest prior trade inside the trailing 24h window
    market_counts: dict = defaultdict(int)  # condition_id -> count of PRIOR trades
    notable_eligible = 0  # prior trades that could be tested by the notable rule
    notable_count = 0  # prior trades that fired the notable rule at their own time

    for i, trade in enumerate(trades):
        price = float(trade.price)
        value = price * float(trade.size)
        condition_id = trade.condition_id

        # Evaluate the notable rule for every trade with at least one prior
        # trade - both for labels (i >= MIN_PRIOR_TRADES) and for the running
        # prior_notable_rate feature. Uses PRIOR stats only.
        is_notable = False
        if i >= 1:
            prior_count = i
            prior_value_mean = value_sum / prior_count
            prior_value_var = max(value_sumsq / prior_count - prior_value_mean**2, 0.0)
            prior_value_std = math.sqrt(prior_value_var)
            if prior_value_std > 0.0:
                is_notable = value > prior_value_mean + 2.0 * prior_value_std
            else:
                is_notable = value > 2.0 * prior_value_mean

        if i >= MIN_PRIOR_TRADES:
            prior_count = i
            prior_price_mean = price_sum / prior_count
            prior_value_mean = value_sum / prior_count
            # Population variance from running sums; clamp tiny negative float error.
            prior_value_var = max(value_sumsq / prior_count - prior_value_mean**2, 0.0)
            prior_value_std = math.sqrt(prior_value_var)
            prior_value_cv = (
                min(prior_value_std / prior_value_mean, MAX_VALUE_CV) if prior_value_mean > 0.0 else 0.0
            )
            value_ratio = value / prior_value_mean if prior_value_mean > 0.0 else 1.0
            log_value_ratio = min(max(math.log(max(value_ratio, 1e-9)), -6.0), 6.0)
            if prior_value_std > 0.0:
                value_zscore = (value - prior_value_mean) / prior_value_std
            elif prior_value_mean > 0.0:
                value_zscore = value_ratio
            else:
                value_zscore = 0.0
            value_zscore = min(max(value_zscore, -10.0), 10.0)

            cutoff = trade.traded_at - timedelta(hours=24)
            while window_start < i and trades[window_start].traded_at < cutoff:
                window_start += 1
            prior_trades_24h = i - window_start

            gap_hours = (trade.traded_at - trades[i - 1].traded_at).total_seconds() / 3600.0
            gap_hours = min(max(gap_hours, 0.0), MAX_GAP_HOURS)

            prior_on_market = market_counts[condition_id]
            notable_rate = notable_count / notable_eligible if notable_eligible > 0 else 0.0

            rows.append(
                [
                    1.0 if trade.side == "YES" else 0.0,
                    price,
                    price - prior_price_mean,
                    math.log1p(prior_value_mean),
                    gap_hours,
                    float(prior_trades_24h),
                    trade.traded_at.hour / 23.0,
                    prior_value_cv,
                    notable_rate,
                    1.0 if prior_on_market == 0 else 0.0,
                    prior_on_market / prior_count,
                    abs(price - 0.5) * 2.0,
                    math.log1p(value),
                    log_value_ratio,
                    value_zscore,
                ]
            )
            labels.append(1.0 if is_notable else 0.0)
            trade_ids.append(trade.trade_id)

        # Fold the current trade into the running prior-stats AFTER its
        # features and label have been computed.
        price_sum += price
        value_sum += value
        value_sumsq += value * value
        market_counts[condition_id] += 1
        if i >= 1:
            notable_eligible += 1
            if is_notable:
                notable_count += 1

    if not rows:
        return np.empty((0, FEATURE_COUNT)), np.empty((0,)), []
    return np.asarray(rows, dtype=np.float64), np.asarray(labels, dtype=np.float64), trade_ids


def build_dataset(session) -> Tuple[np.ndarray, np.ndarray, List[str], List]:
    """Build the full training dataset across all wallets.

    Returns:
        X, y, trade_ids as in build_features_for_wallet, concatenated across
        wallets, plus the traded_at timestamp of each row (for temporal splits).
    """
    all_trades = (
        session.query(Trade)
        .order_by(Trade.wallet_address, Trade.traded_at, Trade.id)
        .all()
    )
    grouped = defaultdict(list)
    for trade in all_trades:
        grouped[trade.wallet_address].append(trade)

    x_parts: List[np.ndarray] = []
    y_parts: List[np.ndarray] = []
    trade_ids: List[str] = []
    timestamps: List = []

    for wallet_trades in grouped.values():
        x_wallet, y_wallet, ids_wallet = build_features_for_wallet(wallet_trades)
        if not ids_wallet:
            continue
        traded_at_by_id = {trade.trade_id: trade.traded_at for trade in wallet_trades}
        x_parts.append(x_wallet)
        y_parts.append(y_wallet)
        trade_ids.extend(ids_wallet)
        timestamps.extend(traded_at_by_id[trade_id] for trade_id in ids_wallet)

    if not x_parts:
        return np.empty((0, FEATURE_COUNT)), np.empty((0,)), [], []
    return np.vstack(x_parts), np.concatenate(y_parts), trade_ids, timestamps
