# Tests the trade scoring model.
"""Tests for app/ml/model.py â€” numpy-only signal model loading and inference."""
import json
import math

import numpy as np
import pytest

from app.ml import model as ml_model
from app.ml.features import FEATURE_COUNT, FEATURE_NAMES
from app.ml.model import SignalModel, get_signal_model, load_model, reset_signal_model_cache


def _valid_payload(**overrides):
    payload = {
        "w": [0.1] * FEATURE_COUNT,
        "b": -0.5,
        "feature_means": [0.0] * FEATURE_COUNT,
        "feature_stds": [1.0] * FEATURE_COUNT,
        "feature_names": FEATURE_NAMES,
        "trained_at": "2026-06-11T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def _write_payload(tmp_path, payload):
    path = tmp_path / "model_weights.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLoadModel:
    def test_returns_none_when_file_missing(self, tmp_path):
        assert load_model(tmp_path / "does_not_exist.json") is None

    def test_returns_none_for_invalid_json(self, tmp_path):
        path = tmp_path / "model_weights.json"
        path.write_text("{not valid json", encoding="utf-8")
        assert load_model(path) is None

    def test_returns_none_for_missing_keys(self, tmp_path):
        path = _write_payload(tmp_path, {"w": [0.1] * FEATURE_COUNT})
        assert load_model(path) is None

    def test_returns_none_for_wrong_feature_width(self, tmp_path):
        path = _write_payload(tmp_path, _valid_payload(w=[0.1, 0.2]))
        assert load_model(path) is None

    def test_returns_none_for_zero_std(self, tmp_path):
        path = _write_payload(tmp_path, _valid_payload(feature_stds=[0.0] * FEATURE_COUNT))
        assert load_model(path) is None

    def test_returns_none_for_non_finite_weights(self, tmp_path):
        path = _write_payload(tmp_path, _valid_payload(w=[float("nan")] * FEATURE_COUNT))
        assert load_model(path) is None

    def test_loads_valid_file(self, tmp_path):
        path = _write_payload(tmp_path, _valid_payload())
        model = load_model(path)
        assert isinstance(model, SignalModel)
        assert model.b == -0.5
        assert model.w.shape == (FEATURE_COUNT,)
        assert model.feature_names == FEATURE_NAMES


class TestPredict:
    def _model(self, w=None, b=0.0, means=None, stds=None):
        return SignalModel(
            w=w if w is not None else [1.0] * FEATURE_COUNT,
            b=b,
            feature_means=means if means is not None else [0.0] * FEATURE_COUNT,
            feature_stds=stds if stds is not None else [1.0] * FEATURE_COUNT,
        )

    def test_returns_values_in_open_unit_interval(self):
        model = self._model()
        X = np.array(
            [
                [0.0] * FEATURE_COUNT,
                [1.0] * FEATURE_COUNT,
                [-1.0] * FEATURE_COUNT,
                [3.0] * FEATURE_COUNT,
            ]
        )
        scores = model.predict(X)
        assert scores.shape == (4,)
        assert np.all(scores > 0.0)
        assert np.all(scores < 1.0)
        assert math.isclose(scores[0], 0.5)  # zero logit -> 0.5

    def test_sigmoid_is_numerically_stable_for_extreme_logits(self):
        model = self._model()
        X = np.array([[1000.0] * FEATURE_COUNT, [-1000.0] * FEATURE_COUNT])
        with np.errstate(over="raise", invalid="raise"):
            scores = model.predict(X)
        assert np.all(np.isfinite(scores))
        assert np.all(scores >= 0.0)
        assert np.all(scores <= 1.0)
        assert scores[0] > 0.999
        assert scores[1] < 0.001

    def test_standardizes_with_stored_means_and_stds(self):
        means = [2.0] * FEATURE_COUNT
        stds = [4.0] * FEATURE_COUNT
        model = self._model(means=means, stds=stds)
        # Raw input equal to the means standardizes to zero -> sigmoid(b) = 0.5
        scores = model.predict(np.array([means]))
        assert math.isclose(scores[0], 0.5)


class TestOptionalStartup:
    def test_get_signal_model_returns_none_without_weights_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ml_model, "DEFAULT_WEIGHTS_PATH", tmp_path / "missing.json")
        reset_signal_model_cache()
        try:
            assert get_signal_model() is None
            assert get_signal_model() is None  # cached miss stays a no-op
        finally:
            reset_signal_model_cache()

    def test_app_starts_and_serves_without_weights_file(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient

        from app.main import create_app

        monkeypatch.setattr(ml_model, "DEFAULT_WEIGHTS_PATH", tmp_path / "missing.json")
        reset_signal_model_cache()
        try:
            app = create_app(lifespan_context=None, csrf_enabled=False)
            assert app.state.signal_model is None
            client = TestClient(app)
            response = client.get("/healthz")
            assert response.status_code == 200
        finally:
            reset_signal_model_cache()
