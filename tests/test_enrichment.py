"""Tests for enrichment batch parsing — handles single-dict and malformed LLM responses."""
import json

from engine.generation.guide_schema import Beat
from engine.generation.script_parser import BeatStub


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
    from engine.generation.beat_enrichment import _enrich_batch
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
    _enrich_batch([stub], "Argentina squad review", CaptureLLM(), "football")

    user_msg = next(m["content"] for m in captured if m["role"] == "user")
    assert "Do not introduce" in user_msg


# ── niche branching (improve-codebase-architecture review, candidate 1) ─────
#
# _is_shallow_beat()/_enrich_batch() used to be hardcoded football-only — a non-
# football standard-path reel naming a specific person got analyzed with football-
# tactics framing and a shallow-beat gate that never matched non-football vocabulary.
# See docs/specs/2026-09-beat-enrichment-niche-branching-module-design.md.

def test_enrich_batch_football_niche_uses_the_football_system_prompt():
    from engine.generation.beat_enrichment import _enrich_batch

    captured = []

    class CaptureLLM:
        def complete(self, messages, **kwargs):
            captured.extend(messages)
            return '[]'

    stub = BeatStub(
        index=0, beat_type="body", section="GOALKEEPER", player="Emiliano Martinez",
        vo_script="Martinez saves penalties consistently.", duration_s=5.0, on_screen_text=[],
    )
    _enrich_batch([stub], "Argentina squad review", CaptureLLM(), "football")

    system_msg = next(m["content"] for m in captured if m["role"] == "system")
    assert "football tactical analyst" in system_msg


def test_enrich_batch_non_football_niche_uses_a_generic_system_prompt():
    from engine.generation.beat_enrichment import _enrich_batch

    captured = []

    class CaptureLLM:
        def complete(self, messages, **kwargs):
            captured.extend(messages)
            return '[]'

    stub = BeatStub(
        index=0, beat_type="body", section="", player="Warren Buffett",
        vo_script="Buffett holds stocks for decades.", duration_s=5.0, on_screen_text=[],
    )
    _enrich_batch([stub], "Investing habits review", CaptureLLM(), "personal finance")

    system_msg = next(m["content"] for m in captured if m["role"] == "system")
    assert "football" not in system_msg.lower()
    assert "personal finance" in system_msg


def test_is_shallow_beat_football_niche_uses_the_football_tactical_regex():
    from engine.generation.beat_enrichment import _is_shallow_beat

    # >=25 words (so the word-count floor alone can't explain the result) containing
    # football-specific tactical markers ("forces", "which means", "press") but none
    # of evaluator.py's _INSIGHT_TACTICAL_UNIVERSAL vocabulary — proves the football
    # branch is actually selected, not the universal one coincidentally matching.
    stub = BeatStub(
        index=0, beat_type="body", section="", player="Emiliano Martinez",
        vo_script=(
            "Martinez forces opponents into costly mistakes under relentless press "
            "and his quick reflexes which means strikers rush shots early instead "
            "of waiting for the better early chance today in front of goal every "
            "single match this entire season long."
        ),
        duration_s=5.0, on_screen_text=[],
    )
    assert _is_shallow_beat(stub, "football") is False


def test_is_shallow_beat_non_football_niche_uses_the_universal_tactical_regex():
    from engine.generation.beat_enrichment import _is_shallow_beat

    # >=25 words containing a word from evaluator.py's _INSIGHT_TACTICAL_UNIVERSAL
    # ("strategy") but none of _TACTICAL_MARKERS' football-specific vocabulary —
    # proves the universal branch is genuinely consulted for a non-football niche,
    # not just that the football regex happens to also match.
    stub = BeatStub(
        index=0, beat_type="body", section="", player="Warren Buffett",
        vo_script=(
            "Buffett's long-term strategy of holding quality businesses through "
            "market downturns has helped him compound wealth steadily for many "
            "decades while most other investors panic and sell far too early "
            "during every single recession."
        ),
        duration_s=5.0, on_screen_text=[],
    )
    assert _is_shallow_beat(stub, "personal finance") is False


def test_is_shallow_beat_non_football_niche_without_universal_markers_is_shallow():
    from engine.generation.beat_enrichment import _is_shallow_beat

    # >=25 words, names a person, but contains neither the football nor the
    # universal tactical vocabulary — must be classified shallow regardless of
    # niche; isolates the "no vocabulary match" path from the separate "too short"
    # path the other two tests already cover.
    stub = BeatStub(
        index=0, beat_type="body", section="", player="Warren Buffett",
        vo_script=(
            "Buffett has been investing in companies for a very long time and "
            "people really admire how calm and patient he always seems to be "
            "during interviews and public appearances every year."
        ),
        duration_s=5.0, on_screen_text=[],
    )
    assert _is_shallow_beat(stub, "personal finance") is True


def test_make_conflict_stub_prompt_has_topic_fence():
    """_make_conflict_stub must include the 'Do not introduce' constraint in the user message."""
    from engine.generation.beat_enrichment import _make_conflict_stub

    captured = []

    class CaptureLLM:
        def complete(self, messages, **kwargs):
            captured.extend(messages)
            return '{"vo_script": "Test.", "visual_direction": "player running"}'

    _make_conflict_stub("Argentina squad review context", CaptureLLM(), index=3)

    user_msg = next(m["content"] for m in captured if m["role"] == "user")
    assert "Do not introduce" in user_msg
