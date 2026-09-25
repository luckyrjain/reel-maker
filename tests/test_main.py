"""Tests for api/main.py's startup lifespan hook."""
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app


def test_startup_logs_a_warning_for_each_misconfigured_model(caplog):
    with patch(
        "api.main.validate_configured_models",
        return_value=["main generation (LLM_MODEL / NVIDIA_GENERATION_MODEL): fake reason"],
    ):
        with caplog.at_level("WARNING"):
            with TestClient(app):
                pass
    assert any("fake reason" in r.message for r in caplog.records)


def test_startup_does_not_crash_when_every_model_checks_out():
    with patch("api.main.validate_configured_models", return_value=[]):
        with TestClient(app) as client:
            resp = client.get("/")
    assert resp.status_code == 200
