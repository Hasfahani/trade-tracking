# Summary: Loads and runs the resolved-market outcome (profitability) model.
# Details: It supports the local machine-learning workflow: numpy-only inference
# for the win-probability model trained by scripts/train_outcome_model.py.
"""Numpy-only inference for the outcome (profitability) model.

Mirrors app.ml.model: weights are trained offline and exported to
``data/outcome_model_weights.json``; the app scores trades with
``sigmoid(X_standardized @ w + b)`` using plain numpy and never imports the
training stack. The weights file is optional - when absent, outcome scoring is
simply disabled and the rest of the app is unaffected.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from app.ml.model import _stable_sigmoid
from app.ml.outcome_features import FEATURE_NAMES

logger = logging.getLogger(__name__)

DEFAULT_OUTCOME_WEIGHTS_PATH = Path("data/outcome_model_weights.json")
DEFAULT_WIN_THRESHOLD = 0.5  # balanced label - flag a likely winner above 0.5

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {"loaded": False, "model": None}


class OutcomeModel:
    """Logistic win-probability scorer over app.ml.outcome_features."""

    def __init__(
        self,
        w,
        b,
        feature_means,
        feature_stds,
        feature_names=None,
        trained_at=None,
        threshold=None,
        metrics=None,
    ):
        self.w = np.asarray(w, dtype=np.float64)
        self.b = float(b)
        self.feature_means = np.asarray(feature_means, dtype=np.float64)
        self.feature_stds = np.asarray(feature_stds, dtype=np.float64)
        self.feature_names = list(feature_names or FEATURE_NAMES)
        self.trained_at = trained_at
        self.threshold = float(threshold) if threshold is not None else DEFAULT_WIN_THRESHOLD
        self.metrics = metrics or {}

    def predict(self, X) -> np.ndarray:
        """Win probability for each row of X (already in feature_names order)."""
        X = np.asarray(X, dtype=np.float64)
        X_std = (X - self.feature_means) / self.feature_stds
        return _stable_sigmoid(X_std @ self.w + self.b)

    def explain(self, x_row) -> List[Tuple[str, float]]:
        """Per-feature logit contributions for one row, largest magnitude first."""
        x = np.asarray(x_row, dtype=np.float64).reshape(-1)
        x_std = (x - self.feature_means) / self.feature_stds
        contributions = self.w * x_std
        pairs = list(zip(self.feature_names, (float(c) for c in contributions)))
        return sorted(pairs, key=lambda pair: abs(pair[1]), reverse=True)


def load_outcome_model(
    path: Union[str, Path] = DEFAULT_OUTCOME_WEIGHTS_PATH
) -> Optional[OutcomeModel]:
    """Load exported outcome weights, returning None if missing or invalid."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        w = np.asarray(payload["w"], dtype=np.float64)
        b = float(payload["b"])
        feature_means = np.asarray(payload["feature_means"], dtype=np.float64)
        feature_stds = np.asarray(payload["feature_stds"], dtype=np.float64)
        feature_names = payload.get("feature_names") or list(FEATURE_NAMES)

        n = len(feature_names)
        unknown = [name for name in feature_names if name not in FEATURE_NAMES]
        if unknown:
            raise ValueError(f"unknown feature names: {unknown}")
        shape = (n,)
        if w.shape != shape or feature_means.shape != shape or feature_stds.shape != shape:
            raise ValueError(f"weight vectors must have length {n}")
        if not (
            np.all(np.isfinite(w))
            and np.isfinite(b)
            and np.all(np.isfinite(feature_means))
            and np.all(np.isfinite(feature_stds))
        ):
            raise ValueError("weights contain non-finite values")
        if np.any(feature_stds == 0.0):
            raise ValueError("feature_stds must be non-zero")

        threshold = payload.get("threshold")
        if threshold is not None:
            threshold = float(threshold)
            if not (np.isfinite(threshold) and 0.0 < threshold < 1.0):
                logger.warning("Ignoring invalid outcome threshold %r - using default", threshold)
                threshold = None

        return OutcomeModel(
            w=w,
            b=b,
            feature_means=feature_means,
            feature_stds=feature_stds,
            feature_names=feature_names,
            trained_at=payload.get("trained_at"),
            threshold=threshold,
            metrics=payload.get("test_metrics"),
        )
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Invalid outcome model weights at %s: %s - scoring disabled", path, exc)
        return None


def get_outcome_model() -> Optional[OutcomeModel]:
    """Return the process-wide OutcomeModel, loading it at most once."""
    if not _cache["loaded"]:
        with _cache_lock:
            if not _cache["loaded"]:
                _cache["model"] = load_outcome_model(DEFAULT_OUTCOME_WEIGHTS_PATH)
                _cache["loaded"] = True
    return _cache["model"]


def reset_outcome_model_cache() -> None:
    """Forget the cached model so the next get_outcome_model() reloads it (tests)."""
    with _cache_lock:
        _cache["model"] = None
        _cache["loaded"] = False
