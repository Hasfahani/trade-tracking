# Summary: Builds leak-safe features + win/loss labels for the outcome model.
# Details: It supports the local machine-learning workflow for the resolved-market
# profitability model: every feature is knowable at trade time, and the label is
# the real outcome from market_resolutions.
"""Feature engineering for the resolved-market outcome (profitability) model.

Unlike the notable-trade model (which predicts a deterministic value-anomaly
rule), this model predicts something genuinely useful and externally grounded:
**did this trade win once its market resolved?** The label comes from
``market_resolutions`` (real YES/NO outcomes from Polymarket's CLOB API), so it
is honest by construction.

Win/loss for a single trade (``side`` encodes BUY->YES / SELL->NO of the
``outcome_token``):
    - BUY of token T  (side == "YES"): win iff the market resolved to T.
    - SELL of token T (side == "NO"):  win iff the market resolved against T.

Every feature is computable from information available **at trade time** - no
feature peeks at the resolution. The dataset is built with a single global
time-ordered sweep so three families of context can be assembled without
temporal leakage:

  * wallet history     - the wallet's prior trading behaviour (expanding window)
  * market context     - cross-wallet activity on the same market BEFORE this
                         trade (Move #3: gets richer as more wallets are tracked)
  * track record       - the wallet's prior win rate, counting only markets that
                         had already *resolved* before this trade happened
                         (time-gated by ``resolved_at`` so it never leaks)
"""

import math
from collections import defaultdict, deque
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from app.models import MarketResolution, Trade

# Feature order is the column order of the matrix built below. The trained model
# records this list in its weights file so inference selects the same columns.
FEATURE_NAMES = [
    "side_yes",                       # 1 if BUY (side==YES) else 0
    "token_yes",                      # 1 if the traded token is YES else 0
    "price",                          # market-implied probability of the token at entry
    "price_extremity",               # |price-0.5|*2 (longshot vs near-certain)
    "market_implied_win_prob",       # market's implied P(this trade wins): price if BUY else 1-price
    "log1p_trade_value",             # log1p(price*size)
    "hour_of_day_norm",              # trade hour / 23
    "day_of_week_norm",              # weekday / 6
    "log1p_prior_trade_count",       # wallet experience before this trade
    "prior_trades_in_24h",           # wallet's recent pace
    "is_first_trade_on_market",      # first time this wallet touched the market
    "market_concentration",          # wallet's prior trades on this market / prior count
    "prior_resolved_win_rate",       # wallet's track record on markets resolved by now
    "log1p_prior_resolved_count",    # how much resolved history backs that win rate
    "trade_value_vs_market_mean",    # this value vs the market's prior mean trade value (capped)
    "log1p_market_prior_trades",     # cross-wallet attention on the market so far
    "log1p_distinct_wallets_on_market",  # distinct tracked wallets on the market so far
]
FEATURE_COUNT = len(FEATURE_NAMES)

FEATURE_LABELS = {
    "side_yes": "buy vs sell",
    "token_yes": "YES vs NO token",
    "price": "market-implied probability at entry",
    "price_extremity": "how extreme the price is (longshot/near-certain)",
    "market_implied_win_prob": "market-implied probability this trade wins",
    "log1p_trade_value": "trade size",
    "hour_of_day_norm": "time of day",
    "day_of_week_norm": "day of week",
    "log1p_prior_trade_count": "wallet's experience",
    "prior_trades_in_24h": "wallet's recent trading pace",
    "is_first_trade_on_market": "first entry on this market",
    "market_concentration": "wallet's focus on this market",
    "prior_resolved_win_rate": "wallet's prior win rate (resolved markets)",
    "log1p_prior_resolved_count": "how much resolved history backs the win rate",
    "trade_value_vs_market_mean": "trade size vs the market's typical size",
    "log1p_market_prior_trades": "how much tracked activity the market had",
    "log1p_distinct_wallets_on_market": "how many tracked wallets are on the market",
}

MIN_PRIOR_TRADES = 0  # outcome model is informative even on a wallet's early trades
MAX_VALUE_RATIO = 20.0  # cap trade_value_vs_market_mean's heavy tail
NEUTRAL_WIN_RATE = 0.5  # prior when the wallet has no resolved history yet


def trade_won(side: str, outcome_token: Optional[str], resolution: str) -> Optional[float]:
    """Return 1.0 if the trade won, 0.0 if it lost, or None if undecidable.

    ``side`` is BUY (YES) / SELL (NO) of ``outcome_token``; ``resolution`` is the
    market's resolved YES/NO outcome.
    """
    if outcome_token not in ("YES", "NO") or resolution not in ("YES", "NO"):
        return None
    bought = side == "YES"
    token_won = resolution == outcome_token
    if bought:
        return 1.0 if token_won else 0.0
    return 1.0 if not token_won else 0.0


def _naive(dt):
    """Drop tzinfo so naive (resolutions) and aware (trades) timestamps compare."""
    if dt is not None and getattr(dt, "tzinfo", None) is not None:
        return dt.replace(tzinfo=None)
    return dt


def build_outcome_dataset(
    session, min_prior_trades: int = MIN_PRIOR_TRADES
) -> Tuple[np.ndarray, np.ndarray, List[str], List]:
    """Build the global outcome training set with a single time-ordered sweep.

    Returns:
        X: float array (n, FEATURE_COUNT).
        y: float array (n,) - 1.0 win / 0.0 loss.
        trade_ids: trade_id per row.
        timestamps: traded_at per row (for temporal splits).

    Only trades on markets resolved YES/NO (and with a YES/NO token) become rows;
    every trade updates the running context so features stay leakage-free.
    """
    resolutions: Dict[str, Tuple[str, Any]] = {}
    for row in session.query(
        MarketResolution.condition_id, MarketResolution.outcome, MarketResolution.resolved_at
    ).filter(MarketResolution.outcome.in_(("YES", "NO"))).all():
        resolutions[row[0]] = (row[1], _naive(row[2]))

    trades = (
        session.query(Trade)
        .order_by(Trade.traded_at.asc(), Trade.id.asc())
        .all()
    )

    # Resolution events to fold into per-wallet track records, in resolved_at
    # order. Only resolved trades that carry a usable resolved_at and label
    # participate (so the feature is strictly time-gated).
    events: List[Tuple[Any, str, float]] = []
    for trade in trades:
        res = resolutions.get(trade.condition_id)
        if res is None or res[1] is None:
            continue
        won = trade_won(trade.side, trade.outcome_token, res[0])
        if won is None:
            continue
        events.append((res[1], trade.wallet_address, won))
    events.sort(key=lambda e: e[0])
    ev_i = 0

    # Per-wallet running state.
    wallet_prior_count: Dict[str, int] = defaultdict(int)
    wallet_recent: Dict[str, deque] = defaultdict(deque)  # traded_at within 24h
    wallet_market_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    wallet_win_count: Dict[str, float] = defaultdict(float)
    wallet_resolved_known: Dict[str, int] = defaultdict(int)

    # Per-market running state (cross-wallet).
    market_count: Dict[str, int] = defaultdict(int)
    market_value_sum: Dict[str, float] = defaultdict(float)
    market_wallets: Dict[str, set] = defaultdict(set)

    rows: List[List[float]] = []
    labels: List[float] = []
    trade_ids: List[str] = []
    timestamps: List = []

    for trade in trades:
        wallet = trade.wallet_address
        condition_id = trade.condition_id
        t = _naive(trade.traded_at)
        price = float(trade.price)
        value = price * float(trade.size)

        # Fold every market resolution that became known strictly before t.
        while ev_i < len(events) and events[ev_i][0] < t:
            _res_at, ev_wallet, ev_won = events[ev_i]
            wallet_resolved_known[ev_wallet] += 1
            wallet_win_count[ev_wallet] += ev_won
            ev_i += 1

        prior_count = wallet_prior_count[wallet]

        # 24h pace: evict timestamps older than 24h, then count what remains.
        recent = wallet_recent[wallet]
        cutoff = t - timedelta(hours=24)
        while recent and recent[0] < cutoff:
            recent.popleft()
        prior_trades_24h = len(recent)

        prior_on_market = wallet_market_counts[wallet][condition_id]
        known = wallet_resolved_known[wallet]
        win_rate = (wallet_win_count[wallet] / known) if known > 0 else NEUTRAL_WIN_RATE

        m_count = market_count[condition_id]
        m_mean = (market_value_sum[condition_id] / m_count) if m_count > 0 else value
        value_ratio = min(value / m_mean, MAX_VALUE_RATIO) if m_mean > 0 else 1.0
        distinct_wallets = len(market_wallets[condition_id])

        res = resolutions.get(condition_id)
        won = trade_won(trade.side, trade.outcome_token, res[0]) if res is not None else None
        emit = won is not None and prior_count >= min_prior_trades
        if emit:
            rows.append(
                [
                    1.0 if trade.side == "YES" else 0.0,
                    1.0 if trade.outcome_token == "YES" else 0.0,
                    price,
                    abs(price - 0.5) * 2.0,
                    price if trade.side == "YES" else 1.0 - price,
                    math.log1p(value),
                    t.hour / 23.0,
                    t.weekday() / 6.0,
                    math.log1p(prior_count),
                    float(prior_trades_24h),
                    1.0 if prior_on_market == 0 else 0.0,
                    (prior_on_market / prior_count) if prior_count > 0 else 0.0,
                    win_rate,
                    math.log1p(known),
                    value_ratio,
                    math.log1p(m_count),
                    math.log1p(distinct_wallets),
                ]
            )
            labels.append(won)
            trade_ids.append(trade.trade_id)
            timestamps.append(t)

        # Fold the current trade into running state AFTER computing its features.
        wallet_prior_count[wallet] = prior_count + 1
        recent.append(t)
        wallet_market_counts[wallet][condition_id] = prior_on_market + 1
        market_count[condition_id] = m_count + 1
        market_value_sum[condition_id] += value
        market_wallets[condition_id].add(wallet)

    if not rows:
        return np.empty((0, FEATURE_COUNT)), np.empty((0,)), [], []
    return (
        np.asarray(rows, dtype=np.float64),
        np.asarray(labels, dtype=np.float64),
        trade_ids,
        timestamps,
    )
