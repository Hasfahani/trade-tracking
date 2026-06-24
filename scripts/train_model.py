#!/usr/bin/env python
# Trains the trade scoring model.
"""Train the notable-trade scoring model. LOCAL-ONLY - never deployed.

IMPORTANT - deployment boundary:
- tensorflow is imported ONLY inside app.ml.train.train_and_export. It must
  never be imported at module top in app code and must NOT be added to
  requirements.txt.
- This script runs on a developer machine against the local database. Only
  the exported weights file (data/model_weights.json) is deployed; the app
  scores trades with plain numpy (app/ml/model.py).
- Install tensorflow locally when needed: pip install tensorflow

This is a thin CLI wrapper around app.ml.train.train_and_export.

Usage (from the repo root):
    python scripts/train_model.py [--epochs 100] [--lecture] [--leaked] [--output PATH]

By default the model is trained on the LEAKAGE-SAFE feature set: the current
trade's value features (which the label is derived from) are excluded, so the
reported metrics are an honest estimate of skill rather than ROC-AUC 1.0.

--lecture trains with the exact lecture math (single sigmoid neuron, MSE
loss, SGD lr=0.1, no class weights). The default "improved" mode keeps the
same neuron but uses binary cross-entropy + class weights so the rare
notable class actually gets learned, and exports a validation-chosen
operating threshold.

--leaked reproduces the OLD leaky baseline on all features (for comparison
only - never deploy it). --output writes elsewhere than the deployed
data/model_weights.json (e.g. a lecture-mode file).
"""
import argparse
import sys

# Ensure repo root is on sys.path when run as a script
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the notable-trade model (local only).")
    parser.add_argument("--epochs", type=int, default=100, help="Maximum epochs; improved mode uses early stopping (default: 100)")
    parser.add_argument(
        "--lecture",
        action="store_true",
        help="Train with the exact lecture setup (MSE + SGD 0.1, no class weights) instead of the improved default.",
    )
    parser.add_argument(
        "--leaked",
        action="store_true",
        help="Train the leaky baseline on ALL features (incl. the label-derived value features). Comparison only - do not deploy.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write weights to this path instead of the deployed data/model_weights.json.",
    )
    args = parser.parse_args()

    from app.ml.features import DEFAULT_FEATURE_NAMES, FEATURE_NAMES
    from app.ml.train import train_and_export

    mode = "lecture" if args.lecture else "improved"
    if args.leaked:
        feature_names = list(FEATURE_NAMES)
        allow_leakage = True
        feature_note = "LEAKY baseline - all features"
    else:
        feature_names = list(DEFAULT_FEATURE_NAMES)
        allow_leakage = False
        feature_note = "leakage-safe"
    output_path = pathlib.Path(args.output) if args.output else None
    header = f"{'epoch':>5} | {'bias':>8} | {'train_loss':>10}"

    def print_progress(epoch, total_epochs, train_loss, weights, bias):
        if epoch == 1:
            print(header)
            print("-" * len(header))
        if epoch <= 10 or epoch % 10 == 0:
            print(f"{epoch:>5} | {bias:8.4f} | {train_loss:10.6f}")

    print(f"Loading dataset and training ({mode} mode, {len(feature_names)} features, {feature_note})...")
    try:
        result = train_and_export(
            epochs=args.epochs,
            mode=mode,
            feature_names=feature_names,
            allow_leakage=allow_leakage,
            output_path=output_path,
            progress_callback=print_progress,
        )
    except (RuntimeError, ValueError) as exc:
        print(str(exc))
        return 1

    val = result["val_metrics"]
    test = result["test_metrics"]
    print()
    print(
        f"Dataset: {result['n_train']} train / {result['n_val']} val / {result['n_test']} test rows "
        "(temporal 70/15/15 split)"
    )
    print(f"Mode: {result['mode']}  loss={result['loss']}  optimizer={result['optimizer']}", end="")
    if result.get("class_weight_positive"):
        print(f"  positive class weight={result['class_weight_positive']:.1f}")
    else:
        print()
    excluded = result.get("excluded_features") or []
    print(
        f"Feature set: {result.get('feature_set')} "
        f"({len(result['feature_names'])} features); excluded: {', '.join(excluded) or 'none'}"
    )
    print(
        f"Epochs: best {result.get('best_epoch', result['epochs_completed'])}, "
        f"completed {result['epochs_completed']} / requested {result['epochs_requested']}"
    )
    print(f"Chosen operating threshold (max F0.5 on validation): {result['threshold']:.4f}")
    print()
    print(f"{'':>12} | {'val':>8} | {'test':>8}")
    for key, label in (
        ("base_rate", "base rate"),
        ("roc_auc", "ROC-AUC"),
        ("pr_auc", "PR-AUC"),
        ("precision_at_threshold", "precision"),
        ("recall_at_threshold", "recall"),
        ("f1_at_threshold", "F1"),
        ("flag_rate_at_threshold", "flag rate"),
    ):
        print(f"{label:>12} | {val[key]:>8.4f} | {test[key]:>8.4f}")
    print()
    print(f"{'threshold':>9} | {'precision':>9} | {'recall':>9} | {'f1':>9}  (test sweep)")
    for threshold_key, row in test["threshold_sweep"].items():
        print(f"{float(threshold_key):>9.4f} | {row['precision']:>9.4f} | {row['recall']:>9.4f} | {row['f1']:>9.4f}")

    print()
    print(f"Saved model weights to {result['output_path']}")
    print("Next: python scripts/score_all_trades.py --overwrite")
    return 0


if __name__ == "__main__":
    sys.exit(main())
