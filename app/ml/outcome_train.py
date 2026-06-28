# Summary: Trains the resolved-market outcome (profitability) model.
# Details: It supports the local machine-learning workflow. Pure-numpy logistic
# regression - no tensorflow - so it trains anywhere the app runs and exports
# the same numpy-scored weights format as the notable model.
"""Training for the outcome (win-probability) model.

This is intentionally a self-contained, pure-numpy logistic regression: the
label is real and roughly balanced, so a calibrated linear model is a strong,
honest, fully explainable baseline that needs no heavy training stack. It reuses
the metric and split helpers from app.ml.train (all numpy; tensorflow is only
imported deep inside that module's notable-model trainer, never here).

Exports ``data/outcome_model_weights.json`` in the same shape app.ml.outcome_model
loads for numpy inference.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import numpy as np

from app.ml.outcome_features import FEATURE_NAMES, build_outcome_dataset
from app.ml.outcome_model import DEFAULT_OUTCOME_WEIGHTS_PATH
from app.ml.train import (
    average_precision,
    precision_recall_f1,
    roc_auc,
    standardize,
    temporal_split,
)

logger = logging.getLogger(__name__)

DEFAULT_EPOCHS = 600
DEFAULT_LR = 0.1
DEFAULT_L2 = 1e-2  # validation sweep favours stronger L2 now the feature set is richer
MIN_ROWS = 30  # below this a temporal split is meaningless


def _sigmoid(z: np.ndarray) -> np.ndarray:
    out = np.empty_like(z)
    pos = z >= 0
    out[pos] = 1.0 / (1.0 + np.exp(-z[pos]))
    ez = np.exp(z[~pos])
    out[~pos] = ez / (1.0 + ez)
    return np.clip(out, 1e-12, 1.0 - 1e-12)


def _train_logreg(
    X_train, y_train, X_val, y_val, epochs, lr, l2, pos_weight,
    progress_callback: Optional[Callable[[int, int, float], None]] = None,
):
    """Gradient-descent logistic regression with class weights + L2.

    Tracks validation average-precision each epoch and restores the best weights
    (a light early-stopping that prevents overfitting the small resolved set).
    """
    n, d = X_train.shape
    w = np.zeros(d, dtype=np.float64)
    b = 0.0
    sample_w = np.where(y_train == 1.0, pos_weight, 1.0)
    sw_sum = float(sample_w.sum())

    best_w, best_b, best_ap = w.copy(), b, -1.0
    for epoch in range(1, epochs + 1):
        p = _sigmoid(X_train @ w + b)
        error = p - y_train
        grad_w = X_train.T @ (sample_w * error) / sw_sum + l2 * w
        grad_b = float((sample_w * error).sum()) / sw_sum
        w -= lr * grad_w
        b -= lr * grad_b

        val_scores = _sigmoid(X_val @ w + b)
        val_ap = average_precision(y_val, val_scores)
        if np.isfinite(val_ap) and val_ap > best_ap:
            best_ap, best_w, best_b = val_ap, w.copy(), b
        if progress_callback is not None and (epoch <= 10 or epoch % 50 == 0):
            train_loss = float(
                -(sample_w * (y_train * np.log(p) + (1 - y_train) * np.log(1 - p))).sum() / sw_sum
            )
            progress_callback(epoch, epochs, train_loss)

    if best_ap >= 0.0:
        return best_w, best_b, best_ap
    return w, b, best_ap


def _select_threshold_max_f1(y_true, y_score):
    """Threshold that maximises F1 on (y_true, y_score). Balanced label -> no flag cap."""
    candidates = np.unique(y_score)
    best_t, best_f1 = 0.5, -1.0
    for c in candidates:
        _, _, f1 = precision_recall_f1(y_true, y_score, c)
        if f1 > best_f1:
            best_f1, best_t = f1, float(c)
    return best_t, max(best_f1, 0.0)


def _metrics(y_true, y_score, threshold) -> Dict[str, float]:
    y_true = np.asarray(y_true, dtype=float)
    y_score = np.asarray(y_score, dtype=float)
    precision, recall, f1 = precision_recall_f1(y_true, y_score, threshold)
    y_pred = (y_score >= threshold).astype(float)
    accuracy = float((y_pred == y_true).mean()) if len(y_true) else 0.0
    brier = float(np.mean((y_score - y_true) ** 2)) if len(y_true) else 0.0
    return {
        "base_rate": float(y_true.mean()) if len(y_true) else 0.0,
        "roc_auc": roc_auc(y_true, y_score),
        "pr_auc": average_precision(y_true, y_score),
        "accuracy": accuracy,
        "brier": brier,
        "precision_at_threshold": precision,
        "recall_at_threshold": recall,
        "f1_at_threshold": f1,
    }


def _baseline_report(y_val, y_test, val_score, test_score) -> Dict[str, Any]:
    """Evaluate a simple score baseline with threshold chosen on validation."""
    threshold, val_f1 = _select_threshold_max_f1(y_val, val_score)
    return {
        "threshold": float(threshold),
        "val_metrics": {**_metrics(y_val, val_score, threshold), "f1_at_selection": float(val_f1)},
        "test_metrics": {**_metrics(y_test, test_score, threshold), "threshold": float(threshold)},
    }


def train_outcome_and_export(
    epochs: int = DEFAULT_EPOCHS,
    lr: float = DEFAULT_LR,
    l2: float = DEFAULT_L2,
    output_path: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, int, float], None]] = None,
) -> Dict[str, Any]:
    """Train the outcome model and export weights + threshold.

    Raises RuntimeError when there is not enough resolved data to train.
    """
    from app.db import get_db_context

    with get_db_context() as session:
        X, y, _ids, timestamps = build_outcome_dataset(session)

    if len(y) < MIN_ROWS:
        raise RuntimeError(
            f"Not enough resolved trades to train: {len(y)} rows (need >= {MIN_ROWS}). "
            "Ingest more wallets and backfill resolutions first "
            "(scripts/bulk_ingest.py, scripts/backfill_resolutions.py --resolutions)."
        )

    X_train_raw, y_train, X_val_raw, y_val, X_test_raw, y_test = temporal_split(X, y, timestamps)
    if len(y_train) == 0 or len(y_val) == 0 or len(y_test) == 0:
        raise RuntimeError(f"Not enough rows for a temporal split (got {len(y)}).")
    X_train, X_val, X_test, means, stds = standardize(X_train_raw, X_val_raw, X_test_raw)

    n_pos = float(y_train.sum())
    n_neg = float(len(y_train) - n_pos)
    pos_weight = (n_neg / n_pos) if n_pos > 0 else 1.0

    w, b, best_val_ap = _train_logreg(
        X_train, y_train, X_val, y_val, epochs, lr, l2, pos_weight, progress_callback
    )

    val_scores = _sigmoid(X_val @ w + b)
    test_scores = _sigmoid(X_test @ w + b)
    threshold, val_f1 = _select_threshold_max_f1(y_val, val_scores)
    val_metrics = _metrics(y_val, val_scores, threshold)
    test_metrics = _metrics(y_test, test_scores, threshold)
    feature_index = {name: i for i, name in enumerate(FEATURE_NAMES)}
    baseline_metrics = {
        "market_implied_win_prob": _baseline_report(
            y_val,
            y_test,
            X_val_raw[:, feature_index["market_implied_win_prob"]],
            X_test_raw[:, feature_index["market_implied_win_prob"]],
        ),
        "prior_resolved_win_rate": _baseline_report(
            y_val,
            y_test,
            X_val_raw[:, feature_index["prior_resolved_win_rate"]],
            X_test_raw[:, feature_index["prior_resolved_win_rate"]],
        ),
    }
    market_test = baseline_metrics["market_implied_win_prob"]["test_metrics"]
    model_vs_market = {
        "roc_auc_delta": float(test_metrics["roc_auc"] - market_test["roc_auc"]),
        "pr_auc_delta": float(test_metrics["pr_auc"] - market_test["pr_auc"]),
        "accuracy_delta": float(test_metrics["accuracy"] - market_test["accuracy"]),
        "brier_delta": float(test_metrics["brier"] - market_test["brier"]),
        "f1_delta": float(test_metrics["f1_at_threshold"] - market_test["f1_at_threshold"]),
    }

    output_path = Path(output_path) if output_path is not None else Path(DEFAULT_OUTCOME_WEIGHTS_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model": "outcome_winprob",
        "w": [float(v) for v in w],
        "b": float(b),
        "feature_means": [float(v) for v in means],
        "feature_stds": [float(v) for v in stds],
        "feature_names": list(FEATURE_NAMES),
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "loss": "weighted_binary_crossentropy",
        "optimizer": f"numpy_gd(lr={lr}, l2={l2})",
        "epochs": int(epochs),
        "class_weight_positive": float(pos_weight),
        "threshold": float(threshold),
        "n_train": int(len(y_train)),
        "n_val": int(len(y_val)),
        "n_test": int(len(y_test)),
        "best_val_pr_auc": float(best_val_ap) if best_val_ap >= 0.0 else None,
        "val_metrics": {**val_metrics, "f1_at_selection": val_f1},
        "test_metrics": {**test_metrics, "threshold": float(threshold)},
        "baseline_metrics": baseline_metrics,
        "model_vs_market": model_vs_market,
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(
        "Outcome model trained (%d rows) -> %s; test ROC-AUC %.4f, accuracy %.4f",
        len(y), output_path, test_metrics["roc_auc"], test_metrics["accuracy"],
    )
    return {**payload, "output_path": str(output_path)}
