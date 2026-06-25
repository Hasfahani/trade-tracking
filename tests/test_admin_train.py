# Summary: Tests the model training admin page.
# Details: It checks this part of the project so future code changes do not silently break expected behavior.
"""Tests for the /admin/train-model page and background training guard."""
import threading
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import create_app
from app.ml import train as ml_train
from app.models import Base


def _build_client(csrf_enabled=False):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    app = create_app(lifespan_context=None, csrf_enabled=csrf_enabled)

    def override_get_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_training_status():
    ml_train.reset_training_status()
    yield
    ml_train.reset_training_status()


def _wait_until_not_running(timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ml_train.get_training_status()["state"] != "running":
            return
        time.sleep(0.05)
    raise AssertionError("training thread did not finish in time")


class TestTrainModelPage:
    def test_page_renders_without_tensorflow(self):
        client = _build_client()
        with patch("app.ml.train.tensorflow_available", return_value=False):
            response = client.get("/admin/train-model")
        assert response.status_code == 200
        assert "Training unavailable on this server" in response.text
        assert "disabled" in response.text

    def test_page_renders_with_tensorflow(self):
        client = _build_client()
        with patch("app.ml.train.tensorflow_available", return_value=True):
            response = client.get("/admin/train-model")
        assert response.status_code == 200
        assert "Train model" in response.text
        assert "Training unavailable" not in response.text

    def test_page_shows_no_model_yet_when_weights_missing(self, tmp_path):
        from app.ml import model as ml_model

        client = _build_client()
        with patch.object(ml_model, "DEFAULT_WEIGHTS_PATH", tmp_path / "missing.json"):
            response = client.get("/admin/train-model")
        assert response.status_code == 200
        assert "No model yet" in response.text

    def test_status_endpoint_idle(self):
        client = _build_client()
        response = client.get("/admin/train-model/status")
        assert response.status_code == 200
        assert response.json() == {"state": "idle"}


class TestStartTraining:
    def test_post_rejected_while_run_active(self):
        client = _build_client()
        release = threading.Event()

        def fake_train(epochs=200, progress_callback=None):
            assert release.wait(timeout=10)
            return {"trained_at": "t", "n_train": 1, "n_test": 1, "test_metrics": {}}

        with patch("app.ml.train.train_and_export", side_effect=fake_train), \
             patch("app.ml.train.tensorflow_available", return_value=True), \
             patch("app.ml.model.get_signal_model", return_value=None):
            first = client.post("/admin/train-model", follow_redirects=False)
            assert first.status_code == 303
            assert "Training%20started" in first.headers["location"]
            assert ml_train.get_training_status()["state"] == "running"

            second = client.post("/admin/train-model", follow_redirects=False)
            assert second.status_code == 303
            assert "already%20in%20progress" in second.headers["location"]

            release.set()
            _wait_until_not_running()
        assert ml_train.get_training_status()["state"] == "done"

    def test_post_blocked_when_tensorflow_unavailable(self):
        client = _build_client()
        with patch("app.ml.train.tensorflow_available", return_value=False):
            response = client.post("/admin/train-model", follow_redirects=False)
        assert response.status_code == 303
        assert "unavailable" in response.headers["location"]
        assert ml_train.get_training_status()["state"] == "idle"

    def test_post_without_csrf_token_is_rejected(self):
        client = _build_client(csrf_enabled=True)
        with patch("app.ml.train.tensorflow_available", return_value=True):
            response = client.post("/admin/train-model", follow_redirects=False)
        assert response.status_code == 403
        assert ml_train.get_training_status()["state"] == "idle"

    def test_training_error_is_reported_not_raised(self):
        client = _build_client()

        def broken_train(epochs=200, progress_callback=None):
            raise RuntimeError("dataset exploded")

        with patch("app.ml.train.train_and_export", side_effect=broken_train), \
             patch("app.ml.train.tensorflow_available", return_value=True):
            response = client.post("/admin/train-model", follow_redirects=False)
            assert response.status_code == 303
            _wait_until_not_running()

        status = ml_train.get_training_status()
        assert status["state"] == "error"
        assert "dataset exploded" in status["error"]

        # Page still renders and shows the failure.
        with patch("app.ml.train.tensorflow_available", return_value=True):
            page = client.get("/admin/train-model")
        assert page.status_code == 200
        assert "Training failed" in page.text

    def test_completion_reloads_model_on_app_state(self, tmp_path):
        from app.ml import model as ml_model

        client = _build_client()
        sentinel = object()

        def fake_train(epochs=200, progress_callback=None):
            return {"trained_at": "t", "n_train": 5, "n_test": 2, "test_metrics": {"base_rate": 0.1}}

        with patch("app.ml.train.train_and_export", side_effect=fake_train), \
             patch("app.ml.train.tensorflow_available", return_value=True), \
             patch("app.ml.model.get_signal_model", return_value=sentinel):
            response = client.post("/admin/train-model", follow_redirects=False)
            assert response.status_code == 303
            _wait_until_not_running()
            assert client.app.state.signal_model is sentinel

        status = ml_train.get_training_status()
        assert status["state"] == "done"
        assert status["result"]["n_train"] == 5
