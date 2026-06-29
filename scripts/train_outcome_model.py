#!/usr/bin/env python
# Summary: Trains the resolved-market outcome (profitability) model.
# Details: It is a command-line helper for setup, maintenance, migration, backup, scoring, or operational checks.
"""Train the outcome (win-probability) model. LOCAL-ONLY.

Predicts whether a trade wins once its market resolves, grounded in real
``market_resolutions`` outcomes. Pure-numpy sigmoid regression - no tensorflow
needed - exporting data/outcome_model_weights.json for numpy inference.

Usage (from the repo root):
    python scripts/train_outcome_model.py [--epochs 400] [--lr 0.1]
                                          [--l2 0.001] [--improved-bce]
                                          [--output PATH]

The more wallets you ingest and the more markets you resolve, the stronger this
model gets - run scripts/bulk_ingest.py and scripts/backfill_resolutions.py
--resolutions first.
"""
import argparse
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description="Train the outcome (profitability) model (local only).")
    parser.add_argument("--epochs", type=int, default=600, help="Gradient-descent epochs (default: 600).")
    parser.add_argument("--lr", type=float, default=0.1, help="Learning rate (default: 0.1).")
    parser.add_argument("--l2", type=float, default=1e-2, help="L2 regularization strength (default: 0.01).")
    parser.add_argument(
        "--improved-bce",
        action="store_true",
        help="Train with the preserved weighted BCE implementation instead of the temporary MSE default.",
    )
    parser.add_argument("--output", default=None, help="Write weights elsewhere than data/outcome_model_weights.json.")
    args = parser.parse_args()

    from app.ml.outcome_train import DEFAULT_LOSS_MODE, train_outcome_and_export

    header = f"{'epoch':>5} | {'train_loss':>10}"

    def print_progress(epoch, total_epochs, train_loss):
        if epoch == 1:
            print(header)
            print("-" * len(header))
        print(f"{epoch:>5} | {train_loss:10.6f}")

    loss_mode = "weighted_bce" if args.improved_bce else DEFAULT_LOSS_MODE
    print(f"Building resolved-market dataset and training the outcome model ({loss_mode} loss)...")
    try:
        result = train_outcome_and_export(
            epochs=args.epochs,
            lr=args.lr,
            l2=args.l2,
            loss_mode=loss_mode,
            output_path=pathlib.Path(args.output) if args.output else None,
            progress_callback=print_progress,
        )
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1

    val = result["val_metrics"]
    test = result["test_metrics"]
    print()
    print(f"Dataset: {result['n_train']} train / {result['n_val']} val / {result['n_test']} test rows "
          "(temporal 70/15/15 split)")
    print(f"Mode: {result.get('mode')}  loss={result.get('loss')}")
    if result.get("class_weight_positive") is not None:
        print(f"Positive class weight: {result['class_weight_positive']:.2f}")
    print(f"Chosen win threshold (max F1 on validation): {result['threshold']:.4f}")
    print()
    print(f"{'':>10} | {'val':>8} | {'test':>8}")
    for key, label in (
        ("base_rate", "base rate"),
        ("roc_auc", "ROC-AUC"),
        ("pr_auc", "PR-AUC"),
        ("accuracy", "accuracy"),
        ("brier", "Brier"),
        ("precision_at_threshold", "precision"),
        ("recall_at_threshold", "recall"),
        ("f1_at_threshold", "F1"),
    ):
        print(f"{label:>10} | {val[key]:>8.4f} | {test[key]:>8.4f}")
    market = (result.get("baseline_metrics") or {}).get("market_implied_win_prob") or {}
    market_test = market.get("test_metrics") or {}
    edge = result.get("model_vs_market") or {}
    if market_test:
        print()
        print("Market-implied baseline (test):")
        print(f"  ROC-AUC {market_test['roc_auc']:.4f}  PR-AUC {market_test['pr_auc']:.4f}  "
              f"F1 {market_test['f1_at_threshold']:.4f}  Brier {market_test['brier']:.4f}")
        print("Model edge vs market baseline (test):")
        print(f"  ROC-AUC {edge['roc_auc_delta']:+.4f}  PR-AUC {edge['pr_auc_delta']:+.4f}  "
              f"F1 {edge['f1_delta']:+.4f}  Brier {edge['brier_delta']:+.4f}")
    print()
    print(f"Saved outcome model weights to {result['output_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
