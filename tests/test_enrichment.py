"""Tests for enrichment batch parsing — handles single-dict and malformed LLM responses."""
import json
import pytest

from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide


# ── coerce_beat_type ──────────────────────────────────────────────────────────

def _make_beat(**kwargs) -> Beat:
    defaults = dict(
        index=0, type="body", duration_s=5.0,
        visual_direction="player running", on_screen_text=["text"], vo_script="some words"
    )
    defaults.update(kwargs)
    return Beat(**defaults)


def test_coerce_outro_to_cta():
    b = _make_beat(type="outro")
    assert b.type == "cta"


def test_coerce_closing_to_cta():
    b = _make_beat(type="closing")
    assert b.type == "cta"


def test_coerce_call_to_action_to_cta():
    b = _make_beat(type="call to action")
    assert b.type == "cta"


def test_coerce_intro_to_hook():
    b = _make_beat(type="intro")
    assert b.type == "hook"


def test_coerce_unknown_to_body():
    b = _make_beat(type="tactical_analysis")
    assert b.type == "body"


def test_coerce_garbage_to_body():
    b = _make_beat(type="some_random_string_xyz")
    assert b.type == "body"


# ── _enrich_batch response parsing ───────────────────────────────────────────

def _parse_enrich_response(raw: str | dict | list) -> dict[int, str]:
    """Inline of the parsing logic from generate.py._enrich_batch."""
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except Exception:
            return {}
    else:
        data = raw

    if isinstance(data, dict):
        items = [data] if "index" in data else next(
            (v for v in data.values() if isinstance(v, list)), []
        )
    else:
        items = data

    result = {}
    for item in items:
        if isinstance(item, dict):
            idx = item.get("index")
            sent = item.get("tactical_sentence", "").strip()
            if idx is not None and sent:
                result[idx] = sent
    return result


def test_enrich_parses_list():
    resp = [{"index": 0, "tactical_sentence": "Great sentence."}, {"index": 1, "tactical_sentence": "Another."}]
    out = _parse_enrich_response(resp)
    assert out[0] == "Great sentence."
    assert out[1] == "Another."


def test_enrich_parses_single_dict():
    resp = {"index": 2, "tactical_sentence": "Single beat response."}
    out = _parse_enrich_response(resp)
    assert out[2] == "Single beat response."


def test_enrich_parses_json_string():
    resp = json.dumps([{"index": 0, "tactical_sentence": "From string."}])
    out = _parse_enrich_response(resp)
    assert out[0] == "From string."


def test_enrich_ignores_missing_sentence():
    resp = [{"index": 0}, {"index": 1, "tactical_sentence": "Good."}]
    out = _parse_enrich_response(resp)
    assert 0 not in out
    assert out[1] == "Good."


def test_enrich_ignores_junk_elements():
    resp = [{"index": 0, "tactical_sentence": "Fine."}, "not a dict", None, 42]
    out = _parse_enrich_response(resp)
    assert out[0] == "Fine."
    assert len(out) == 1


def test_enrich_returns_empty_on_malformed_json():
    out = _parse_enrich_response("{{invalid json}")
    assert out == {}


def test_enrich_handles_nested_list_in_dict():
    resp = {"results": [{"index": 3, "tactical_sentence": "Nested."}]}
    out = _parse_enrich_response(resp)
    assert out[3] == "Nested."


# ── topic fence in prompt ─────────────────────────────────────────────────

def test_enrich_batch_prompt_has_topic_fence():
    """_enrich_batch must include the 'Do not introduce' constraint in the user message."""
    from worker.tasks.generate import _enrich_batch
    from engine.generation.script_parser import BeatStub

    captured = []

    class CaptureLLM:
        def complete(self, messages, **kwargs):
            captured.extend(messages)
            return '[]'

    stub = BeatStub(
        index=0, beat_type="body", section="GOALKEEPER", player="Emiliano Martinez",
        vo_script="Martinez saves penalties consistently.", duration_s=5.0, on_screen_text=[],
    )
    _enrich_batch([stub], "Argentina squad review", CaptureLLM())

    user_msg = next(m["content"] for m in captured if m["role"] == "user")
    assert "Do not introduce" in user_msg


def test_make_conflict_stub_prompt_has_topic_fence():
    """_make_conflict_stub must include the 'Do not introduce' constraint in the user message."""
    from worker.tasks.generate import _make_conflict_stub

    captured = []

    class CaptureLLM:
        def complete(self, messages, **kwargs):
            captured.extend(messages)
            return '{"vo_script": "Test.", "visual_direction": "player running"}'

    _make_conflict_stub("Argentina squad review context", CaptureLLM(), index=3)

    user_msg = next(m["content"] for m in captured if m["role"] == "user")
    assert "Do not introduce" in user_msg
