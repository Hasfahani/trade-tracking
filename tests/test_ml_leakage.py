# Tests leakage-safe features, the training guard, and subset-width models.
"""Phase 1 ML-integrity tests.

The training label is a deterministic 2-sigma threshold on the current trade's
value (== value_zscore_capped > 2.0). These tests pin down that the honest model
excludes the current-value features, that training refuses to re-introduce them,
and that a narrower (subset) model loads and scores correctly through column
selection.
"""
import json

import numpy as np
import pytest

from app.ml.features import (
    FEATURE_NAMES,
    LABEL_FEATURE,
    LEAKAGE_FEATURE_NAMES,
    SAFE_FEATURE_NAMES,
    select_feature_columns,
)
from app.ml.model import load_model
from app.ml.train import assert_leakage_safe


class TestSafeFeatureSet:
    def test_label_feature_is_a_leak(self):
        assert LABEL_FEATURE in LEAKAGE_FEATURE_NAMES

    def test_safe_set_excludes_every_current_value_feature(self):
        for name in LEAKAGE_FEATURE_NAMES:
            assert name not in SAFE_FEATURE_NAMES
        # The three current-value features are exactly what's dropped.
        assert set(FEATURE_NAMES) - set(SAFE_FEATURE_NAMES) == set(LEAKAGE_FEATURE_NAMES)
        assert len(SAFE_FEATURE_NAMES) == len(FEATURE_NAMES) - 3

    def test_safe_set_preserves_full_order(self):
        # Safe set is the full order with the leaks removed (so column indices line up).
        assert SAFE_FEATURE_NAMES == [n for n in FEATURE_NAMES if n not in LEAKAGE_FEATURE_NAMES]


class TestSelectFeatureColumns:
    def test_subsets_named_columns_in_order(self):
        X = np.arange(15, dtype=float).reshape(1, 15)
        out = select_feature_columns(X, SAFE_FEATURE_NAMES)
        assert out.shape == (1, len(SAFE_FEATURE_NAMES))
        # The three leak columns are the last three of FEATURE_NAMES.
        np.testing.assert_array_equal(out[0], np.arange(12, dtype=float))

    def test_full_set_returns_input_unchanged(self):
        X = np.zeros((3, 15))
        assert select_feature_columns(X, FEATURE_NAMES) is X
        assert select_feature_columns(X, []) is X

    def test_unknown_feature_raises(self):
        with pytest.raises(KeyError):
            select_feature_columns(np.zeros((1, 15)), ["not_a_feature"])


class TestLeakageGuard:
    def test_safe_set_passes(self):
        assert_leakage_safe(SAFE_FEATURE_NAMES)  # no raise

    def test_full_set_with_label_feature_raises(self):
        with pytest.raises(ValueError, match="leakage"):
            assert_leakage_safe(FEATURE_NAMES)

    def test_weaker_value_leak_alone_raises(self):
        with pytest.raises(ValueError, match="leakage"):
            assert_leakage_safe(SAFE_FEATURE_NAMES + ["log1p_trade_value"])

    def test_allow_leakage_bypasses_the_guard(self):
        assert_leakage_safe(FEATURE_NAMES, allow_leakage=True)  # no raise


def _safe_payload(**overrides):
    n = len(SAFE_FEATURE_NAMES)
    payload = {
        "w": [0.1] * n,
        "b": -0.5,
        "feature_means": [0.0] * n,
        "feature_stds": [1.0] * n,
        "feature_names": list(SAFE_FEATURE_NAMES),
        "trained_at": "2026-06-23T00:00:00+00:00",
        "feature_set": "leakage_safe",
        "leakage_safe": True,
    }
    payload.update(overrides)
    return payload


class TestSubsetWidthModel:
    def test_loads_twelve_feature_safe_model(self, tmp_path):
        path = tmp_path / "weights.json"
        path.write_text(json.dumps(_safe_payload()), encoding="utf-8")
        model = load_model(path)
        assert model is not None
        assert model.feature_names == list(SAFE_FEATURE_NAMES)
        assert model.w.shape == (len(SAFE_FEATURE_NAMES),)

    def test_predict_full_selects_safe_columns(self, tmp_path):
        path = tmp_path / "weights.json"
        path.write_text(json.dumps(_safe_payload()), encoding="utf-8")
        model = load_model(path)

        X_full = np.random.default_rng(0).normal(size=(5, 15))
        scores_full = model.predict_full(X_full)
        # Equivalent to selecting the safe columns by hand and predicting.
        scores_manual = model.predict(X_full[:, :12])
        np.testing.assert_allclose(scores_full, scores_manual)
        assert scores_full.shape == (5,)
        assert np.all((scores_full > 0.0) & (scores_full < 1.0))

    def test_explain_full_returns_only_safe_features(self, tmp_path):
        path = tmp_path / "weights.json"
        path.write_text(json.dumps(_safe_payload()), encoding="utf-8")
        model = load_model(path)

        contributions = model.explain_full(np.ones(15))
        names = {name for name, _ in contributions}
        assert names == set(SAFE_FEATURE_NAMES)
        for leak in LEAKAGE_FEATURE_NAMES:
            assert leak not in names

    def test_rejects_width_mismatch_against_feature_names(self, tmp_path):
        path = tmp_path / "weights.json"
        # 12 feature names but only 2 weights -> invalid.
        path.write_text(json.dumps(_safe_payload(w=[0.1, 0.2])), encoding="utf-8")
        assert load_model(path) is None

    def test_rejects_unknown_feature_name(self, tmp_path):
        path = tmp_path / "weights.json"
        bad_names = list(SAFE_FEATURE_NAMES[:-1]) + ["bogus_feature"]
        path.write_text(json.dumps(_safe_payload(feature_names=bad_names)), encoding="utf-8")
        assert load_model(path) is None


class TestDeployedWeightsAreHonest:
    def test_deployed_model_excludes_leaking_features(self):
        """If a model is deployed, it must not carry any label-leaking feature."""
        from app.ml.model import DEFAULT_WEIGHTS_PATH

        try:
            payload = json.loads(DEFAULT_WEIGHTS_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pytest.skip("no deployed weights file")
        names = payload.get("feature_names") or []
        for leak in LEAKAGE_FEATURE_NAMES:
            assert leak not in names, f"deployed model leaks {leak!r}"
