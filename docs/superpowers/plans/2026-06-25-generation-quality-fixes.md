# Generation Quality Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three quality failures in the reel generation pipeline: context drift (structured scripts generating wrong topics), disconnected beat-level audio, and insight enrichment introducing off-topic events.

**Architecture:** Four surgical changes across four files — a structured-script guard in the context enrichment task, a topic-fence constraint in two LLM prompt strings in the generation task, an audio fade applied per beat in the compositor, and a VO-anchoring sentence in the visual direction system prompt. No new tables, tasks, or dependencies.

**Tech Stack:** Python, Celery, MoviePy 2.x, FFmpeg, pytest

## Global Constraints

- Match existing code style exactly: single-quoted strings, f-strings for interpolation, `_log = logging.getLogger(__name__)` convention
- Never set `.status` directly on a model — always use `transition()`
- MoviePy 2.x API: `audio_fadein` / `audio_fadeout` are methods on `AudioFileClip`; `with_start()` is the positioning method
- All tests use `unittest.mock` (no pytest-mock): `patch()` as context manager, `MagicMock()` for objects
- Run the full test suite with `.venv/bin/pytest` before each commit; all 91 tests must pass

---

### Task 1: Structured script guard in `enrich_context.py`

**Files:**
- Modify: `worker/tasks/enrich_context.py`
- Test: `tests/test_enrich_context_task.py`

**Interfaces:**
- Produces: `_is_structured_script(text: str) -> bool` — importable pure function used in tests

- [ ] **Step 1: Write the failing tests**

Add these three tests to the bottom of `tests/test_enrich_context_task.py`:

```python
# ── structured script guard ───────────────────────────────────────────────

def test_is_structured_script_detects_three_caps_headers():
    from worker.tasks.enrich_context import _is_structured_script
    text = "GOALKEEPER\nMartinez is world class.\nDEFENSE\nRomero leads the line.\nMIDFIELD\nDe Paul is the engine."
    assert _is_structured_script(text) is True


def test_is_structured_script_rejects_free_text():
    from worker.tasks.enrich_context import _is_structured_script
    text = "Argentina are the best team in the world. Messi is the greatest. The squad looks strong."
    assert _is_structured_script(text) is False


def test_enrichment_skipped_for_structured_script_even_below_threshold():
    """llm_enrich must not be called when context has ≥3 ALL-CAPS headers, even when score < 60."""
    from worker.tasks.enrich_context import enrich_context
    structured_ctx = "GOALKEEPER\nMartinez saves penalties.\nDEFENSE\nRomero leads.\nMIDFIELD\nDe Paul runs."
    job = _make_job()
    reel = _make_reel(context=structured_ctx)
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else reel

    with (
        patch("worker.tasks.enrich_context.SessionLocal", return_value=db),
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(25, ["too_short"])),
        patch("worker.tasks.enrich_context.llm_enrich") as mock_enrich,
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.generate_guide"),
        patch("worker.tasks.enrich_context.transition"),
        patch("worker.tasks.enrich_context.record_stage"),
    ):
        enrich_context(1)

    mock_enrich.assert_not_called()
    assert reel.enriched_context is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_enrich_context_task.py::test_is_structured_script_detects_three_caps_headers tests/test_enrich_context_task.py::test_is_structured_script_rejects_free_text tests/test_enrich_context_task.py::test_enrichment_skipped_for_structured_script_even_below_threshold -v
```

Expected: FAIL with `ImportError: cannot import name '_is_structured_script'`

- [ ] **Step 3: Implement the guard**

In `worker/tasks/enrich_context.py`:

**a) Add `import re` after `import logging`:**

```python
import logging
import re
from datetime import datetime, timezone
```

**b) Add the helper function after the `_log` line (line 14):**

```python
_CAPS_HEADER = re.compile(r'^[A-Z][A-Z\s:\-]{2,}$')


def _is_structured_script(text: str) -> bool:
    """Return True when text contains ≥3 ALL-CAPS section headers (structured script)."""
    return sum(1 for line in text.splitlines() if _CAPS_HEADER.match(line.strip())) >= 3
```

**c) Replace the enrichment block (the `if score < ENRICH_THRESHOLD:` section, currently lines 47–69) with:**

```python
        # ── Step 2: Enrich if below threshold (skip for structured scripts) ────
        is_structured = _is_structured_script(reel.context)
        if score < ENRICH_THRESHOLD and not is_structured:
            _heartbeat(db, job, 40)
            llm = get_enrichment_provider()
            enrich_provider = "nvidia" if settings.nvidia_api_key else "ollama"
            with record_stage(db, reel.id, "context_enrich", provider=enrich_provider) as ev:
                enriched = llm_enrich(reel.context, reel.niche or "", llm)
                ev.detail["score_before"] = score
                ev.detail["issues"] = issues

            if enriched:
                reel.enriched_context = enriched
                job.meta = {**(job.meta or {}), "enriched": True}
                _log.info("reel_id=%s: context enriched (score was %d)", reel.id, score)
            else:
                job.meta = {**(job.meta or {}), "enriched": False, "enrich_failed": True}
                _log.warning(
                    "reel_id=%s: llm_enrich returned None (score %d), proceeding with original",
                    reel.id, score,
                )
        else:
            skipped_reason = "structured_script" if is_structured else "score_above_threshold"
            job.meta = {**(job.meta or {}), "enriched": False, "enrich_skipped": skipped_reason}
            _log.info(
                "reel_id=%s: skipping enrichment (%s, score=%d)",
                reel.id, skipped_reason, score,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_enrich_context_task.py -v
```

Expected: 9 tests PASS (6 existing + 3 new)

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/pytest
```

Expected: 94 tests PASS

- [ ] **Step 6: Commit**

```bash
git add worker/tasks/enrich_context.py tests/test_enrich_context_task.py
git commit -m "fix: skip llm_enrich for structured scripts to prevent context drift"
```

---

### Task 2: Topic fence on insight enrichment prompts in `generate.py`

**Files:**
- Modify: `worker/tasks/generate.py`
- Test: `tests/test_enrichment.py`

**Interfaces:**
- Consumes: `_enrich_batch(batch, context, llm)` from `worker/tasks/generate.py` — existing function, tests import it directly

- [ ] **Step 1: Write the failing test**

Add this test to the bottom of `tests/test_enrichment.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_enrichment.py::test_enrich_batch_prompt_has_topic_fence tests/test_enrichment.py::test_make_conflict_stub_prompt_has_topic_fence -v
```

Expected: FAIL with `AssertionError` (constraint not yet in prompt)

- [ ] **Step 3: Add topic fence to `_enrich_batch` prompt**

In `worker/tasks/generate.py`, find `_enrich_batch` (around line 176). Replace the user message content in `messages`:

Current:
```python
        {"role": "user", "content": (
            f"CONTEXT:\n{context[:800]}\n\n"
            "For each beat write ONE sentence: what the player ENABLES tactically, "
            "not how they feel. Use specific mechanisms.\n\n"
            "BAD: 'Romero defends with passion.'\n"
            "GOOD: 'Romero's line-stepping lets Argentina defend 15 yards higher, "
            "creating turnovers in dangerous zones.'\n\n"
            f"Beats:\n[\n{beat_lines}\n]\n\n"
            'Return: [{"index": 0, "tactical_sentence": "..."}, ...]'
        )},
```

Replace with:
```python
        {"role": "user", "content": (
            f"CONTEXT:\n{context[:800]}\n\n"
            "For each beat write ONE sentence: what the player ENABLES tactically, "
            "not how they feel. Use specific mechanisms.\n\n"
            "BAD: 'Romero defends with passion.'\n"
            "GOOD: 'Romero's line-stepping lets Argentina defend 15 yards higher, "
            "creating turnovers in dangerous zones.'\n\n"
            "IMPORTANT: Only reference players, events, and facts already present in "
            "each beat. Do not introduce matches, tournaments, scorelines, or players "
            "not mentioned in the beat text.\n\n"
            f"Beats:\n[\n{beat_lines}\n]\n\n"
            'Return: [{"index": 0, "tactical_sentence": "..."}, ...]'
        )},
```

- [ ] **Step 4: Add topic fence to `_make_conflict_stub` prompt**

In the same file, find `_make_conflict_stub` (around line 239). Replace the user message content:

Current:
```python
        {"role": "user", "content": (
            f"CONTEXT:\n{context[:1200]}\n\n"
            "Write 2-3 sentences of voiceover identifying ONE genuine weakness, risk, or "
            "challenge for this squad. Be specific and factual. Conversational, not academic.\n\n"
            "Also write a visual_direction (max 12 words) that an editor can use to source footage. "
            "Start with a player's full name if one is relevant.\n\n"
            '{"vo_script": "...", "visual_direction": "..."}'
        )},
```

Replace with:
```python
        {"role": "user", "content": (
            f"CONTEXT:\n{context[:1200]}\n\n"
            "Write 2-3 sentences of voiceover identifying ONE genuine weakness, risk, or "
            "challenge for this squad. Be specific and factual. Conversational, not academic.\n\n"
            "IMPORTANT: Only reference players, events, and challenges present in the CONTEXT "
            "above. Do not introduce matches, tournaments, scorelines, or players not mentioned "
            "in the context.\n\n"
            "Also write a visual_direction (max 12 words) that an editor can use to source footage. "
            "Start with a player's full name if one is relevant.\n\n"
            '{"vo_script": "...", "visual_direction": "..."}'
        )},
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_enrichment.py -v
```

Expected: 11 tests PASS (9 existing + 2 new)

- [ ] **Step 6: Run full suite**

```bash
.venv/bin/pytest
```

Expected: 96 tests PASS

- [ ] **Step 7: Commit**

```bash
git add worker/tasks/generate.py tests/test_enrichment.py
git commit -m "fix: add topic fence to insight enrichment and conflict stub prompts"
```

---

### Task 3: Audio crossfade at beat boundaries in `compositor.py`

**Files:**
- Modify: `engine/render/compositor.py`

*No unit test — the MoviePy audio pipeline is too tightly coupled to internals to mock meaningfully. Verified by running a render end-to-end and listening to beat transitions.*

- [ ] **Step 1: Apply the audio fade change**

In `engine/render/compositor.py`, find `composite_cut()` (around line 287). Locate the `AudioFileClip` block inside the beat loop (around lines 313–320):

Current:
```python
        if vo_path and vo_path.exists():
            try:
                track = AudioFileClip(str(vo_path)).with_start(t)
                if track.duration > duration:
                    track = track.subclipped(0, duration).with_start(t)
                vo_tracks.append(track)
            except Exception:
                pass
```

Replace with:
```python
        if vo_path and vo_path.exists():
            try:
                track = AudioFileClip(str(vo_path))
                if track.duration > duration:
                    track = track.subclipped(0, duration)
                track = track.audio_fadein(0.12).audio_fadeout(0.12).with_start(t)
                vo_tracks.append(track)
            except Exception:
                pass
```

The order matters: subclip first (to avoid fading a portion that will be cut), then fade, then position with `with_start(t)`.

- [ ] **Step 2: Run full suite to verify no regressions**

```bash
.venv/bin/pytest
```

Expected: 96 tests PASS

- [ ] **Step 3: Commit**

```bash
git add engine/render/compositor.py
git commit -m "fix: add 120ms audio fadein/fadeout at beat boundaries for smooth narration"
```

---

### Task 4: Visual direction prompt anchoring in `prompt.py`

**Files:**
- Modify: `engine/generation/prompt.py`
- Test: `tests/test_audio_text_sync.py`

- [ ] **Step 1: Write the failing test**

Add this test to the bottom of `tests/test_audio_text_sync.py`:

```python
# ── visual direction prompt anchoring ─────────────────────────────────────

def test_build_visuals_system_message_anchors_to_beat_vo():
    """build_visuals_messages system prompt must instruct the LLM to use only beat VO content."""
    from engine.generation.prompt import build_visuals_messages
    beats = [{"index": 0, "beat_type": "hook", "duration_s": 3.0,
              "vo_script": "Argentina looking strong this tournament.", "player": ""}]
    messages = build_visuals_messages(beats, "football")
    system_msg = next(m["content"] for m in messages if m["role"] == "system")
    assert "ONLY" in system_msg
    assert "explicitly" in system_msg
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/pytest tests/test_audio_text_sync.py::test_build_visuals_system_message_anchors_to_beat_vo -v
```

Expected: FAIL with `AssertionError` (current system message doesn't contain "explicitly")

- [ ] **Step 3: Update the system message in `build_visuals_messages()`**

In `engine/generation/prompt.py`, find the `system` variable inside `build_visuals_messages()` (around line 265):

Current:
```python
    system = (
        "You are a video director assigning specific footage search queries. "
        "Output ONLY a JSON array. No markdown, no commentary."
    )
```

Replace with:
```python
    system = (
        "You are a video director assigning specific footage search queries. "
        "Output ONLY a JSON array. No markdown, no commentary. "
        "For each beat, derive visual_direction ONLY from the specific players, "
        "actions, and events named in that beat's VO. Do not use the global context "
        "or topic to infer additional visual content beyond what the VO explicitly mentions."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_audio_text_sync.py -v
```

Expected: All tests PASS (including new one)

- [ ] **Step 5: Run full suite**

```bash
.venv/bin/pytest
```

Expected: 97 tests PASS

- [ ] **Step 6: Commit**

```bash
git add engine/generation/prompt.py tests/test_audio_text_sync.py
git commit -m "fix: anchor visual direction LLM to beat VO content only"
```
