#!/usr/bin/env python
# Trains the trade scoring model.
"""Train the notable-trade scoring model. LOCAL-ONLY â€” never deployed.

IMPORTANT â€” deployment boundary:
- tensorflow is imported ONLY inside app.ml.train.train_and_export. It must
  never be imported at module top in app code and must NOT be added to
  requirements.txt.
- This script runs on a developer machine against the local database. Only
  the exported weights file (data/model_weights.json) is deployed; the app
  scores trades with plain numpy (app/ml/model.py).
- Install tensorflow locally when needed: pip install tensorflow

This is a thin CLI wrapper around app.ml.train.train_and_export.

Usage (from the repo root):
    python scripts/train_model.py [--epochs 200] [--lecture]

--lecture trains with the exact lecture math (single sigmoid neuron, MSE
loss, SGD lr=0.1, no class weights). The default "improved" mode keeps the
same neuron but uses binary cross-entropy + class weights so the rare
notable class actually gets learned, and exports a validation-chosen
operating threshold.
"""
import argparse
import sys

# Ensure repo root is on sys.path when run as a script
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the notable-trade model (local only).")
    parser.add_argument("--epochs", type=int, default=200, help="Number of training epochs (default: 200)")
    parser.add_argument(
        "--lecture",
        action="store_true",
        help="Train with the exact lecture setup (MSE + SGD 0.1, no class weights) instead of the improved default.",
    )
    args = parser.parse_args()

    from app.ml.features import FEATURE_COUNT
    from app.ml.train import SWEEP_THRESHOLDS, train_and_export

    mode = "lecture" if args.lecture else "improved"
    header = f"{'epoch':>5} | {'bias':>8} | {'train_loss':>10}"

    def print_progress(epoch, total_epochs, train_loss, weights, bias):
        if epoch == 1:
            print(header)
            print("-" * len(header))
        if epoch <= 10 or epoch % 10 == 0:
            print(f"{epoch:>5} | {bias:8.4f} | {train_loss:10.6f}")

    print(f"Loading dataset and training ({mode} mode, {FEATURE_COUNT} features)...")
    try:
        result = train_and_export(epochs=args.epochs, mode=mode, progress_callback=print_progress)
    except RuntimeError as exc:
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
