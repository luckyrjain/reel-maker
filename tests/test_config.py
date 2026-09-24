"""Tests for api/config.py — Settings.

evaluator_axis_weight_multipliers is the first dict-typed Settings field in this
codebase (every other field is str/int/float/bool). pydantic-settings parses a
dict-typed field from its environment variable as JSON, which is untested
territory here — assert the env var round-trips to a real dict, not a string,
before relying on it anywhere else (worker/tasks/generate.py, evaluator.py).
"""
from api.config import Settings


def test_evaluator_axis_weight_multipliers_defaults_to_empty_dict():
    settings = Settings()
    assert settings.evaluator_axis_weight_multipliers == {}
    assert isinstance(settings.evaluator_axis_weight_multipliers, dict)


def test_evaluator_axis_weight_multipliers_env_var_round_trips_to_a_real_dict(monkeypatch):
    monkeypatch.setenv("EVALUATOR_AXIS_WEIGHT_MULTIPLIERS", '{"insight": 0.5, "cta": 0.0}')
    settings = Settings()
    assert settings.evaluator_axis_weight_multipliers == {"insight": 0.5, "cta": 0.0}
    assert isinstance(settings.evaluator_axis_weight_multipliers, dict)
    # Not a JSON string masquerading as "truthy" — values must be real floats.
    assert isinstance(settings.evaluator_axis_weight_multipliers["insight"], float)
