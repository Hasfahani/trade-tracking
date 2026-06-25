# Summary: Loads and runs the trade scoring model.
# Details: It supports the local machine-learning workflow for feature creation, model training, scoring, or explanations.
"""Numpy-only inference for the notable-trade model.

Weights are trained offline by scripts/train_model.py (tensorflow, LOCAL-ONLY)
and exported to data/model_weights.json. The app never imports tensorflow; it
scores trades with sigmoid(X_standardized @ w + b) using plain numpy.

The weights file is optional: the app starts and runs normally without it,
and scoring is simply disabled.
"""

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np

from app.ml.features import FEATURE_COUNT, FEATURE_NAMES

logger = logging.getLogger(__name__)

DEFAULT_WEIGHTS_PATH = Path("data/model_weights.json")

# Fallback flag threshold when the weights file predates threshold selection
# or no model is loaded. Trained models carry their own validation-chosen
# operating threshold in the weights file ("threshold" key).
SIGNAL_THRESHOLD = 0.7

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {"loaded": False, "model": None}


def _stable_sigmoid(z: np.ndarray) -> np.ndarray:
    """Numerically stable sigmoid: never exponentiates a positive argument."""
    z = np.asarray(z, dtype=np.float64)
    out = np.empty_like(z)
    positive = z >= 0
    out[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    exp_z = np.exp(z[~positive])
    out[~positive] = exp_z / (1.0 + exp_z)
    epsilon = np.finfo(np.float64).eps
    return np.clip(out, epsilon, 1.0 - epsilon)


class SignalModel:
    """Logistic scorer over the features produced by app.ml.features."""

    def __init__(
        self,
        w,
        b,
        feature_means,
        feature_stds,
        feature_names=None,
        trained_at=None,
        threshold=None,
        mode=None,
        metrics=None,
    ):
        self.w = np.asarray(w, dtype=np.float64)
        self.b = float(b)
        self.feature_means = np.asarray(feature_means, dtype=np.float64)
        self.feature_stds = np.asarray(feature_stds, dtype=np.float64)
        self.feature_names = list(feature_names or [])
        self.trained_at = trained_at
        self.threshold = float(threshold) if threshold is not None else SIGNAL_THRESHOLD
        self.mode = mode
        self.metrics = metrics or {}
        # Column indices into the full FEATURE_NAMES matrix, when this model was
        # trained on a subset (e.g. the leakage-safe set). None means full-width.
        self._select_idx: Optional[List[int]] = None
        if self.feature_names and self.feature_names != list(FEATURE_NAMES):
            if all(name in FEATURE_NAMES for name in self.feature_names):
                self._select_idx = [FEATURE_NAMES.index(name) for name in self.feature_names]

    def predict(self, X) -> np.ndarray:
        """Standardize with the stored train-set stats and return sigmoid scores.

        X must already have one column per stored weight. Use predict_full when
        you have the full FEATURE_NAMES matrix and need column selection.
        """
        X = np.asarray(X, dtype=np.float64)
        X_std = (X - self.feature_means) / self.feature_stds
        return _stable_sigmoid(X_std @ self.w + self.b)

    def select_columns(self, X_full) -> np.ndarray:
        """Subset a full (n, FEATURE_COUNT) matrix to this model's feature columns."""
        X_full = np.asarray(X_full, dtype=np.float64)
        if self._select_idx is None:
            return X_full
        return X_full[:, self._select_idx]

    def predict_full(self, X_full) -> np.ndarray:
        """Predict from the full FEATURE_NAMES matrix, selecting columns first."""
        return self.predict(self.select_columns(X_full))

    def explain_full(self, x_full_row) -> List[Tuple[str, float]]:
        """Per-feature contributions for one full FEATURE_NAMES row."""
        x = np.asarray(x_full_row, dtype=np.float64).reshape(1, -1)
        return self.explain(self.select_columns(x)[0])

    def explain(self, x_row) -> List[Tuple[str, float]]:
        """Per-feature logit contributions for one feature row.

        Returns (feature_name, w_i * x_standardized_i) pairs sorted by
        absolute contribution, largest first. Positive values push the score
        toward "notable", negative toward "normal".
        """
        x = np.asarray(x_row, dtype=np.float64).reshape(-1)
        x_std = (x - self.feature_means) / self.feature_stds
        contributions = self.w * x_std
        names = self.feature_names or [f"feature_{i}" for i in range(len(contributions))]
        pairs = list(zip(names, (float(c) for c in contributions)))
        return sorted(pairs, key=lambda pair: abs(pair[1]), reverse=True)


def load_model(path: Union[str, Path] = DEFAULT_WEIGHTS_PATH) -> Optional[SignalModel]:
    """Load exported weights, returning None if the file is missing or invalid."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        w = np.asarray(payload["w"], dtype=np.float64)
        b = float(payload["b"])
        feature_means = np.asarray(payload["feature_means"], dtype=np.float64)
        feature_stds = np.asarray(payload["feature_stds"], dtype=np.float64)
        feature_names = payload.get("feature_names")
        # A model may be trained on a subset of the full feature set (the
        # leakage-safe set has 12 of 15). The width is whatever the trained
        # feature_names list says; legacy files without names must be full-width.
        if feature_names is not None:
            n_features = len(feature_names)
            unknown = [name for name in feature_names if name not in FEATURE_NAMES]
            if unknown:
                raise ValueError(f"unknown feature names: {unknown}")
        else:
            n_features = FEATURE_COUNT
        if not (1 <= n_features <= FEATURE_COUNT):
            raise ValueError(f"feature count must be 1..{FEATURE_COUNT}, got {n_features}")
        expected_shape = (n_features,)
        if w.shape != expected_shape or feature_means.shape != expected_shape or feature_stds.shape != expected_shape:
            raise ValueError(f"weight vectors must have length {n_features} (from feature_names)")
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
                logger.warning("Ignoring invalid stored threshold %r - using default", threshold)
                threshold = None

        return SignalModel(
            w=w,
            b=b,
            feature_means=feature_means,
            feature_stds=feature_stds,
            feature_names=payload.get("feature_names"),
            trained_at=payload.get("trained_at"),
            threshold=threshold,
            mode=payload.get("mode"),
            metrics=payload.get("test_metrics"),
        )
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Invalid signal model weights at %s: %s - scoring disabled", path, exc)
        return None


def get_signal_model() -> Optional[SignalModel]:
    """Return the process-wide SignalModel, loading it at most once (None if unavailable)."""
    if not _cache["loaded"]:
        with _cache_lock:
            if not _cache["loaded"]:
                _cache["model"] = load_model(DEFAULT_WEIGHTS_PATH)
                _cache["loaded"] = True
    return _cache["model"]


def effective_signal_threshold() -> float:
    """The operating threshold for flagging trades.

    Uses the validation-selected threshold stored with the trained weights
    when a model is loaded, otherwise the SIGNAL_THRESHOLD default.
    """
    model = get_signal_model()
    if model is not None:
        return model.threshold
    return SIGNAL_THRESHOLD


def reset_signal_model_cache() -> None:
    """Forget the cached model so the next get_signal_model() reloads it (used by tests)."""
    with _cache_lock:
        _cache["model"] = None
        _cache["loaded"] = False
