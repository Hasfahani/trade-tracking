# Trains the local trade scoring model.
"""Training orchestration for the notable-trade model.

tensorflow is a heavy, LOCAL-ONLY dependency: it is imported inside
train_and_export, never at module top, so the app imports this module and
starts normally on servers without TF. Use tensorflow_available() to check
before offering training in the UI.

Two training modes share the SAME architecture â€” the single sigmoid neuron
from the lecture (Manually Written Lecture AP SS26):

    weighted inputs:  z = wÂ·x + b              -> Dense(1) layer
    activation:       y = F(z) = sigmoid(z)    -> activation='sigmoid'
    weight update:    w_new = w + Î±Â·(t âˆ’ y)Â·x  -> gradient descent step
    epochs:           repeat over the dataset  -> the epoch loop below

- "lecture" mode reproduces the lecture training exactly: MSE loss
  (E = 1/N Î£ (t âˆ’ y)Â²) minimised with SGD(learning_rate=0.1), no class
  weighting â€” the same math as the hand-worked epochs in the notes.
- "improved" mode (default) keeps the neuron but trains it with binary
  cross-entropy and class weights. With a ~3.5% positive rate, MSE+sigmoid
  has vanishing gradients near yâ‰ˆ0 and converges to "predict 0 everywhere"
  (precision/recall 0.0). BCE is the maximum-likelihood loss for a sigmoid
  output, and weighting positives by n_neg/n_pos makes rare notable trades
  matter as much as common normal ones.

The operating threshold is not hardcoded: it is chosen on a temporal
validation split (max F1) and exported with the weights.

A single background training run at a time is enforced by the module-level
status registry (get_training_status / start_training_in_background).
"""

import importlib.util
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_EPOCHS = 100
DEFAULT_MODE = "improved"  # "improved" | "lecture"
TRAIN_FRACTION = 0.7
VAL_FRACTION = 0.15  # remainder (0.15) is the test set
SWEEP_THRESHOLDS = (0.3, 0.5, 0.7)
MAX_THRESHOLD_CANDIDATES = 512
EARLY_STOPPING_PATIENCE = 15
EARLY_STOPPING_MIN_EPOCHS = 20
EARLY_STOPPING_MIN_DELTA = 1e-4

_status_lock = threading.Lock()
_status: Dict[str, Any] = {"state": "idle"}  # idle | running | done | error


def tensorflow_available() -> bool:
    """Return whether tensorflow is installed, without importing it."""
    try:
        return importlib.util.find_spec("tensorflow") is not None
    except Exception:
        return False


def temporal_split(X, y, timestamps):
    """Sort all rows by traded_at; 70% train, 15% validation, 15% test. Never random."""
    order = np.argsort(np.array(timestamps, dtype="datetime64[us]"), kind="stable")
    X, y = X[order], y[order]
    train_end = int(len(y) * TRAIN_FRACTION)
    val_end = int(len(y) * (TRAIN_FRACTION + VAL_FRACTION))
    return (
        X[:train_end], y[:train_end],
        X[train_end:val_end], y[train_end:val_end],
        X[val_end:], y[val_end:],
    )


def standardize(X_train, *X_others):
    """Standardize with TRAIN-set statistics only; apply the same transform to the rest."""
    means = X_train.mean(axis=0)
    stds = X_train.std(axis=0)
    stds = np.where(stds == 0.0, 1.0, stds)  # constant features pass through unscaled
    scaled = tuple((X - means) / stds for X in (X_train, *X_others))
    return (*scaled, means, stds)


def precision_recall_f1(y_true, y_score, threshold):
    y_pred = (np.asarray(y_score) >= threshold).astype(float)
    y_true = np.asarray(y_true, dtype=float)
    tp = float(np.sum((y_pred == 1.0) & (y_true == 1.0)))
    fp = float(np.sum((y_pred == 1.0) & (y_true == 0.0)))
    fn = float(np.sum((y_pred == 0.0) & (y_true == 1.0)))
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return precision, recall, f1


def roc_auc(y_true, y_score) -> float:
    """Rank-based ROC-AUC (Mannâ€“Whitney U) with average ranks for ties.

    Returns nan when either class is absent.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_score = np.asarray(y_score, dtype=float)
    pos = y_true == 1.0
    n_pos = int(pos.sum())
    n_neg = int(len(y_true) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return float("nan")

    order = np.argsort(y_score, kind="mergesort")
    sorted_scores = y_score[order]
    ranks_sorted = np.arange(1, len(y_score) + 1, dtype=float)
    i = 0
    while i < len(sorted_scores):
        j = i
        while j + 1 < len(sorted_scores) and sorted_scores[j + 1] == sorted_scores[i]:
            j += 1
        if j > i:
            ranks_sorted[i : j + 1] = (ranks_sorted[i] + ranks_sorted[j]) / 2.0
        i = j + 1
    ranks = np.empty_like(ranks_sorted)
    ranks[order] = ranks_sorted
    u = ranks[pos].sum() - n_pos * (n_pos + 1) / 2.0
    return float(u / (n_pos * n_neg))


def average_precision(y_true, y_score) -> float:
    """PR-AUC as average precision. Returns nan when there are no positives."""
    y_true = np.asarray(y_true, dtype=float)
    y_score = np.asarray(y_score, dtype=float)
    n_pos = float(y_true.sum())
    if n_pos == 0:
        return float("nan")
    order = np.argsort(-y_score, kind="mergesort")
    y_sorted = y_true[order]
    tp_cum = np.cumsum(y_sorted)
    precision_at_k = tp_cum / np.arange(1, len(y_sorted) + 1)
    return float((precision_at_k * y_sorted).sum() / n_pos)


THRESHOLD_FBETA = 0.5  # weights precision 2x recall â€” flags should be rare and high-confidence
MAX_FLAG_RATE = 0.10  # a "notable" flag may fire on at most this fraction of trades


def select_threshold(y_true, y_score, beta: float = THRESHOLD_FBETA, max_flag_rate: float = MAX_FLAG_RATE):
    """Pick the operating threshold that maximises F-beta on (y_true, y_score).

    Two product constraints are encoded here rather than hardcoding a number:
    - beta < 1 weights precision over recall â€” we prefer fewer,
      higher-confidence flags over catching every positive.
    - candidates are restricted to thresholds that flag at most
      max_flag_rate of trades: a "notable trade" pill on half the feed is
      noise, whatever its F-score. (The validation split has only a few
      dozen positives, so without this cap the F surface is flat and the
      search drifts to recall-heavy thresholds that don't generalize.)

    Candidates are the observed scores (subsampled by quantile above
    MAX_THRESHOLD_CANDIDATES). Ties prefer the higher threshold. Falls back
    to 0.5 when no threshold yields any true positives.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_score = np.asarray(y_score, dtype=float)
    candidates = np.unique(y_score)
    if len(candidates) > MAX_THRESHOLD_CANDIDATES:
        candidates = np.unique(np.quantile(candidates, np.linspace(0.0, 1.0, MAX_THRESHOLD_CANDIDATES)))
    allowed = [c for c in candidates if (y_score >= c).mean() <= max_flag_rate]
    if allowed:
        candidates = allowed

    beta_sq = beta * beta
    best_threshold, best_fbeta = 0.5, 0.0
    for candidate in candidates:
        precision, recall, _ = precision_recall_f1(y_true, y_score, candidate)
        denominator = beta_sq * precision + recall
        fbeta = (1 + beta_sq) * precision * recall / denominator if denominator > 0 else 0.0
        if fbeta > best_fbeta or (fbeta == best_fbeta and fbeta > 0.0 and candidate > best_threshold):
            best_threshold, best_fbeta = float(candidate), fbeta
    if best_fbeta == 0.0:
        return 0.5, 0.0
    return best_threshold, best_fbeta


def _metrics_at(y_true, y_score, threshold) -> Dict[str, float]:
    precision, recall, f1 = precision_recall_f1(y_true, y_score, threshold)
    y_score = np.asarray(y_score, dtype=float)
    return {
        "base_rate": float(np.asarray(y_true, dtype=float).mean()) if len(y_true) else 0.0,
        "roc_auc": roc_auc(y_true, y_score),
        "pr_auc": average_precision(y_true, y_score),
        "precision_at_threshold": precision,
        "recall_at_threshold": recall,
        "f1_at_threshold": f1,
        "flag_rate_at_threshold": float((y_score >= threshold).mean()) if len(y_score) else 0.0,
    }


def train_and_export(
    epochs: int = DEFAULT_EPOCHS,
    mode: str = DEFAULT_MODE,
    progress_callback: Optional[Callable[[int, int, float, np.ndarray, float], None]] = None,
) -> Dict[str, Any]:
    """Train the model and export weights + threshold to data/model_weights.json.

    Args:
        epochs: Number of training epochs.
        mode: "improved" (BCE + class weights, default) or "lecture"
            (exact lecture math: MSE + SGD(0.1), no class weights).
        progress_callback: Called after each epoch with
            (epoch, total_epochs, train_loss, flat_weights, bias).

    Returns:
        The saved JSON payload plus an ``output_path`` key.

    Raises:
        ImportError: tensorflow is not installed.
        RuntimeError: not enough data to train.
        ValueError: unknown mode.
    """
    if mode not in ("improved", "lecture"):
        raise ValueError(f"Unknown training mode: {mode!r}")

    import tensorflow as tf  # LOCAL-ONLY heavy dep â€” see module docstring

    from app.db import get_db_context
    from app.ml import model as ml_model
    from app.ml.features import FEATURE_COUNT, FEATURE_NAMES, build_dataset

    with get_db_context() as session:
        X, y, _trade_ids, timestamps = build_dataset(session)
    if len(y) == 0:
        raise RuntimeError("No training data: no wallet has enough trade history.")

    X_train, y_train, X_val, y_val, X_test, y_test = temporal_split(X, y, timestamps)
    if len(y_train) == 0 or len(y_val) == 0 or len(y_test) == 0:
        raise RuntimeError(f"Not enough rows for a temporal train/val/test split (got {len(y)} total).")
    X_train, X_val, X_test, means, stds = standardize(X_train, X_val, X_test)

    # The lecture's neuron: weighted inputs + bias through a sigmoid.
    model = tf.keras.Sequential([tf.keras.layers.Dense(1, activation='sigmoid', input_shape=(FEATURE_COUNT,))])

    class_weight = None
    pos_weight = None
    if mode == "lecture":
        # Exact lecture training: MSE loss, plain SGD with learning rate 0.1.
        model.compile(optimizer=tf.keras.optimizers.SGD(learning_rate=0.1), loss='mse')
        loss_name = "mse"
        optimizer_name = "sgd(0.1)"
    else:
        n_pos = float(y_train.sum())
        n_neg = float(len(y_train) - n_pos)
        if n_pos == 0:
            raise RuntimeError("Training set has no notable trades â€” cannot class-weight.")
        pos_weight = n_neg / n_pos
        class_weight = {0: 1.0, 1: pos_weight}
        model.compile(optimizer=tf.keras.optimizers.Adam(learning_rate=0.01), loss='binary_crossentropy')
        loss_name = "binary_crossentropy"
        optimizer_name = "adam(0.01)"

    best_weights = None
    best_val_ap = -1.0
    best_epoch = 0
    epochs_without_improvement = 0
    epochs_completed = 0
    for epoch in range(1, epochs + 1):
        history = model.fit(X_train, y_train, epochs=1, verbose=0, class_weight=class_weight)
        epochs_completed = epoch
        if mode == "improved":
            epoch_val_scores = model.predict(X_val, verbose=0).flatten()
            epoch_val_ap = average_precision(y_val, epoch_val_scores)
            if epoch_val_ap > best_val_ap + EARLY_STOPPING_MIN_DELTA:
                best_val_ap = epoch_val_ap
                best_epoch = epoch
                best_weights = model.get_weights()
                epochs_without_improvement = 0
            else:
                epochs_without_improvement += 1
        if progress_callback is not None:
            weights, bias = model.layers[0].get_weights()
            progress_callback(
                epoch, epochs, float(history.history["loss"][0]), weights.flatten(), float(bias[0])
            )
        if (
            mode == "improved"
            and epoch >= EARLY_STOPPING_MIN_EPOCHS
            and epochs_without_improvement >= EARLY_STOPPING_PATIENCE
        ):
            break

    if best_weights is not None:
        model.set_weights(best_weights)

    weights, bias = model.layers[0].get_weights()
    val_scores = model.predict(X_val, verbose=0).flatten()
    test_scores = model.predict(X_test, verbose=0).flatten()

    # Operating threshold: chosen on validation (max F0.5), evaluated on test.
    threshold, val_fbeta = select_threshold(y_val, val_scores)
    val_metrics = _metrics_at(y_val, val_scores, threshold)
    test_metrics = _metrics_at(y_test, test_scores, threshold)

    # Legacy fixed-threshold numbers, kept for the admin page and comparisons.
    precision_05, recall_05, f1_05 = precision_recall_f1(y_test, test_scores, 0.5)
    sweep = {}
    for sweep_threshold in (*SWEEP_THRESHOLDS, round(threshold, 4)):
        p, r, f = precision_recall_f1(y_test, test_scores, sweep_threshold)
        sweep[str(sweep_threshold)] = {"precision": p, "recall": r, "f1": f}

    output_path = Path(ml_model.DEFAULT_WEIGHTS_PATH)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "w": [float(v) for v in weights.flatten()],
        "b": float(bias[0]),
        "feature_means": [float(v) for v in means],
        "feature_stds": [float(v) for v in stds],
        "feature_names": FEATURE_NAMES,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "loss": loss_name,
        "optimizer": optimizer_name,
        "epochs_requested": int(epochs),
        "epochs_completed": int(epochs_completed),
        "best_epoch": int(best_epoch or epochs_completed),
        "best_val_pr_auc": float(best_val_ap) if best_val_ap >= 0.0 else None,
        "class_weight_positive": pos_weight,
        "threshold": float(threshold),
        "n_train": int(len(y_train)),
        "n_val": int(len(y_val)),
        "n_test": int(len(y_test)),
        "val_metrics": {**val_metrics, "fbeta_at_selection": val_fbeta},
        "test_metrics": {
            **test_metrics,
            "threshold": float(threshold),
            "precision_at_0_5": precision_05,
            "recall_at_0_5": recall_05,
            "f1_at_0_5": f1_05,
            "threshold_sweep": sweep,
        },
    }
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.info(
        "Model trained (%s mode, %d epochs) and exported to %s â€” threshold %.4f, test F1 %.4f",
        mode, epochs, output_path, threshold, test_metrics["f1_at_threshold"],
    )
    return {**payload, "output_path": str(output_path)}


def get_training_status() -> Dict[str, Any]:
    """Return a snapshot of the current/most recent training run."""
    with _status_lock:
        return dict(_status)


def start_training_in_background(
    epochs: int = DEFAULT_EPOCHS,
    on_success: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> bool:
    """Start one background training run; returns False if one is already running."""
    with _status_lock:
        if _status.get("state") == "running":
            return False
        _status.clear()
        _status.update(
            {
                "state": "running",
                "epoch": 0,
                "total_epochs": epochs,
                "train_mse": None,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    thread = threading.Thread(target=_run_training, args=(epochs, on_success), name="train-model", daemon=True)
    thread.start()
    return True


def _run_training(epochs: int, on_success: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    def _progress(epoch, total_epochs, train_loss, _weights, _bias):
        with _status_lock:
            # Key kept as "train_mse" for the status JSON contract; holds the
            # active mode's training loss (BCE in improved mode).
            _status.update({"epoch": epoch, "total_epochs": total_epochs, "train_mse": train_loss})

    try:
        result = train_and_export(epochs=epochs, progress_callback=_progress)
        with _status_lock:
            _status.update(
                {
                    "state": "done",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "result": {
                        "trained_at": result.get("trained_at"),
                        "mode": result.get("mode"),
                        "threshold": result.get("threshold"),
                        "n_train": result.get("n_train"),
                        "n_val": result.get("n_val"),
                        "n_test": result.get("n_test"),
                        "test_metrics": result.get("test_metrics"),
                    },
                }
            )
        if on_success is not None:
            try:
                on_success(result)
            except Exception:
                logger.exception("Post-training callback failed")
    except Exception as exc:
        logger.exception("Model training failed")
        with _status_lock:
            _status.update(
                {
                    "state": "error",
                    "error": str(exc),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )


def reset_training_status() -> None:
    """Reset the registry to idle (used by tests)."""
    with _status_lock:
        _status.clear()
        _status["state"] = "idle"
