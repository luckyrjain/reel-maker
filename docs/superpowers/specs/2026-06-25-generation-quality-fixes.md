# Generation Quality Fixes — Design Spec

**Date:** 2026-06-25

---

## Problem statement

Three user-reported quality failures:

1. **Context drift** — a structured squad-review script produced a reel about the World Cup Final instead of the squad. The `llm_enrich()` step transformed the topic.
2. **Disconnected audio** — each beat's TTS is a hard-cut audio file; beat boundaries feel like stops rather than a flowing narration.
3. **Insight enrichment drift** — `_enrich_with_insight()` introduces players, matches, and tournaments not mentioned in the beat's own VO, because it draws from the global context.

---

## Approach

Approach A — four targeted surgical fixes. No new tables, no new tasks, no new dependencies. All changes are prompt strings or a 2-line audio filter addition.

---

## Fix 1 — Skip `llm_enrich()` for structured scripts

**File:** `worker/tasks/enrich_context.py`

**Root cause:** `llm_enrich()` runs on any context with score < 60, including well-formed structured scripts. Adding "stakes and a hook angle" to a squad-review script causes the enrichment LLM to introduce the World Cup Final (the highest-stakes Argentina context it knows). The enriched context then poisons every downstream LLM call.

**Fix:** Before calling `llm_enrich()`, detect whether the context is a structured script by checking for ≥3 lines matching the ALL-CAPS header pattern (`^[A-Z][A-Z\s:\-]+$`). If detected, skip enrichment and leave `reel.enriched_context` as `None`. The context score is still computed and stored in `job.meta["context_score"]` for observability.

**Detection logic** (mirrors `script_parser.parse()` detection):
```python
import re
_CAPS_HEADER = re.compile(r'^[A-Z][A-Z\s:\-]{2,}$')

def _is_structured_script(text: str) -> bool:
    return sum(1 for line in text.splitlines() if _CAPS_HEADER.match(line.strip())) >= 3
```

**Outcome:** Structured scripts pass through `enrich_context` untouched. The `generate_guide` task uses `reel.context` verbatim (since `enriched_context` is None).

---

## Fix 2 — Topic fence on insight enrichment

**File:** `worker/tasks/generate.py`

**Root cause:** `_enrich_with_insight()` sends each beat to the enrichment LLM with a generic "add one tactical insight" instruction. The LLM uses the global context for inspiration and inserts references to events not in the beat (e.g. adding "as we saw in the 2022 World Cup Final" to a player beat that never mentioned the final).

**Fix:** Append a hard constraint to the enrichment prompt:

> "Only reference players, events, statistics, and facts already present in the beat above. Do not introduce matches, tournaments, scorelines, years, or players that are not explicitly mentioned in this beat's text."

Apply the same constraint to `_make_conflict_stub()` which builds a conflict beat from the global context — anchor it to entities already present in the parsed beats, not the enriched context.

---

## Fix 3 — Audio crossfade at beat boundaries

**File:** `engine/render/compositor.py`

**Root cause:** Each beat's TTS audio is a separate `AudioFileClip` hard-cut into the composite. The listener hears a dead stop at every beat boundary, making the narration feel like separate disconnected chunks.

**Fix:** After loading each beat's `AudioFileClip`, apply:
- `audio_fadein(0.12)` — 120ms fade in at beat start
- `audio_fadeout(0.12)` — 120ms fade out at beat end

MoviePy's built-in methods handle this in the same compositing pass — no FFmpeg filter or extra pass needed. 120ms is short enough that no VO words are lost; it only smooths the boundary.

```python
audio_clip = AudioFileClip(tts_path)
audio_clip = audio_clip.audio_fadein(0.12).audio_fadeout(0.12)
```

---

## Fix 4 — Visual direction prompt anchoring

**File:** `engine/generation/prompt.py`

**Root cause:** `build_visuals_messages()` provides both the global context and per-beat VO to the LLM. The system message doesn't prevent the LLM from using the global context to invent visuals not grounded in the beat's actual VO content.

**Fix:** Add an explicit instruction to the system message in `build_visuals_messages()`:

> "For each beat, derive `visual_direction` ONLY from the specific players, actions, locations, and events named in that beat's `vo_script`. Do not use the global context to infer or add visual content beyond what the VO explicitly mentions."

---

## Files changed

| File | Change |
|---|---|
| `worker/tasks/enrich_context.py` | Add `_is_structured_script()` guard; skip `llm_enrich()` when detected |
| `worker/tasks/generate.py` | Add topic-fence sentence to `_enrich_with_insight()` and `_make_conflict_stub()` prompts |
| `engine/render/compositor.py` | Add `audio_fadein(0.12).audio_fadeout(0.12)` to each beat's AudioFileClip |
| `engine/generation/prompt.py` | Add anchoring sentence to `build_visuals_messages()` system message |

---

## What this does NOT change

- No DB schema changes
- No new Celery tasks
- No new config values
- `llm_enrich()` itself is unchanged — it still runs for free-form short contexts
- The evaluator is unchanged
- The render pipeline is unchanged except the audio fade

---

## Success criteria

- A structured squad-review script generates a reel about the squad, not a tangential event
- Beat-to-beat narration transitions feel smooth rather than choppy
- Insight enrichment sentences stay within the beat's own subject matter
- Visual directions reference the players and actions named in the VO
