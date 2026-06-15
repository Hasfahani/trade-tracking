# Scores one trade using wallet history.
"""On-demand scoring of a single trade with the local model (numpy only).

Used by the AI analysis layer so the "Analyze" button works even for trades
that were ingested before the model existed: the trade is scored live from
the wallet's history instead of requiring a separate backfill run.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.ml.features import MIN_PRIOR_TRADES, build_features_for_wallet
from app.ml.model import get_signal_model
from app.models import Trade


@dataclass
class TradeScore:
    score: float
    prior_trades: int
    contributions: List[Tuple[str, float]] = field(default_factory=list)


def prior_trade_count(trade: Trade, db: Session) -> int:
    """Number of the wallet's trades strictly before this one."""
    return (
        db.query(Trade)
        .filter(
            Trade.wallet_address == trade.wallet_address,
            Trade.traded_at < trade.traded_at,
        )
        .count()
    )


def score_trade(trade: Trade, db: Session) -> Optional[TradeScore]:
    """Score one trade with the loaded model, or None if not possible.

    Returns None when no model is loaded or the wallet has fewer than
    MIN_PRIOR_TRADES trades before this one (the feature builder skips
    such trades).
    """
    model = get_signal_model()
    if model is None:
        return None

    wallet_trades = (
        db.query(Trade)
        .filter(Trade.wallet_address == trade.wallet_address)
        .order_by(Trade.traded_at.asc(), Trade.id.asc())
        .all()
    )
    X, _, trade_ids = build_features_for_wallet(wallet_trades)
    try:
        row_index = trade_ids.index(trade.trade_id)
    except ValueError:
        return None

    x_row = X[row_index]
    score = float(model.predict(x_row.reshape(1, -1))[0])
    ordered = sorted(wallet_trades, key=lambda t: t.traded_at)
    prior = next(
        (i for i, t in enumerate(ordered) if t.trade_id == trade.trade_id),
        MIN_PRIOR_TRADES,
    )
    return TradeScore(score=score, prior_trades=prior, contributions=model.explain(x_row))
