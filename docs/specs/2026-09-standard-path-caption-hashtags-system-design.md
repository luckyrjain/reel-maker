# System design: standard LLM path caption/hashtags are not content-aware

**Status:** Proposed. **Severity:** Low per `docs/roadmap.md`'s Open Issues table.

## 1. Problem

`docs/roadmap.md`'s Open Issues table: *"Standard LLM path caption/hashtags not content-aware —
Generated from niche only, not actual beat content; structured path uses
`_generate_caption_hashtags()` from actual VO — standard path could do the same."*

The standard LLM path (`worker/tasks/generate.py::generate_guide()`'s `if guide is None:` block)
generates an entire `MasterGuide` — every beat's `vo_script`, `visual_direction`, `on_screen_text`,
**and** `caption`/`hashtags` — in a single JSON completion via `engine/generation/prompt.py::
build_messages()`. The prompt's system/user messages give the model the topic, niche, and platform
target lengths, and a worked example, but no instruction telling it to derive `caption`/`hashtags`
specifically from the beats it is about to write — they're just two more fields in the same JSON
schema, generated in the same pass as everything else, with no explicit content-grounding
instruction. The structured-script path (`_generate_from_structured_script()`) does this
correctly already: after its beats are finalized, `_generate_caption_hashtags()` makes a dedicated
follow-up call passing the beats' actual `vo_script` text, so the caption/hashtags are demonstrably
grounded in what the video actually says.

## 2. What the current code actually does

- `_generate_caption_hashtags(stubs: list[BeatStub], niche: str, llm) -> tuple[str, list[str]]`
  (`worker/tasks/generate.py`) joins `stubs`' `vo_script` text, asks the LLM for a caption + exactly
  15 hashtags, and returns `("", [])` on any ordinary failure (bad JSON, too few hashtags) — but
  re-raises `SoftTimeLimitExceeded` rather than swallowing a runtime-limit breach (an explicit,
  already-tested behavior — see `tests/test_generate_task.py::
  test_generate_caption_hashtags_does_not_swallow_the_soft_time_limit`).
- Its one call site, `_generate_from_structured_script()`, already has real `BeatStub` objects at
  hand — the structured path never runs `build_messages()` at all, so `caption`/`hashtags` are pure
  narrative content this function alone produces.
- The standard path (`if guide is None:` block, `for attempt in range(3):` loop) writes
  `candidate.caption`/`candidate.hashtags` directly from the single-shot `MasterGuide.model_validate_
  json(raw)` parse — no second pass, no explicit content-grounding step.
- `generate_hook_variants()` (`engine/generation/hook_variants.py`) is the closest existing
  precedent for a **standard-path-only**, best-effort, budget-gated follow-up call: one call after
  the guide is accepted, gated on `paid_call_count(db, reel.id) < settings.max_paid_llm_calls_per_reel`,
  `[]` (a no-op default) on any failure, applied identically across every platform guide in
  `guide.cuts` (this codebase's own documented convention: "platforms normally share identical
  beats"). This is the shape to mirror.

## 3. Design decisions

**Generalize `_generate_caption_hashtags()` to take `vo_scripts: list[str]` instead of `stubs:
list[BeatStub]`, so both paths can call it.** The function's only use of `stubs` was `" ".join(s.vo_
script for s in stubs if s.vo_script)` — a plain list of strings is the actual minimal contract, and
decoupling from `BeatStub` (a structured-script-path-only type) lets the standard path pass real
`Beat.vo_script` values (a different Pydantic type, same field name) without constructing throwaway
`BeatStub` wrappers just to satisfy a type the function doesn't otherwise need. The structured path's
one call site becomes `_generate_caption_hashtags([s.vo_script for s in stubs], niche, enrichment_llm)`
— a one-line, behavior-preserving change (existing tests for this function already pass `[]` directly,
so their calling convention doesn't even change).

**One extra best-effort call after the standard-path guide is accepted, mirroring `generate_hook_
variants()`'s shape exactly.** Placed at the end of the `if guide is None:` block, after the
best-of-3 acceptance logic (so it runs on both a threshold-clearing success and a best-of-3 fallback
accept), gated on the same `paid_call_count(db, reel.id) < settings.max_paid_llm_calls_per_reel`
budget check `generate_hook_variants()`'s own call site already uses. Uses `guide.cuts[0].beats`'
`vo_script` values — the same "platforms share identical beats" assumption `hook_variants` already
relies on for its own hook-beat lookup, not a new one introduced here.

**On success, overwrite every platform guide's `caption`/`hashtags`; on failure, leave the
single-shot guide's own values untouched.** This is the one place this design deliberately diverges
from the structured path's shape: the structured path's guide starts with NO caption/hashtags before
`_generate_caption_hashtags()` runs, so its failure path falls back to a hardcoded template
(`f"{niche.title()} breakdown..."`). The standard path already has real, non-empty caption/hashtags
from the main JSON generation before this new step ever runs — degrading to nothing, or to the
structured path's football-specific hardcoded template (wrong niche entirely for a non-football
reel), would make a failed regeneration attempt actively worse than not attempting it. Checking
`if new_caption and new_hashtags:` before overwriting means a failure is a genuine no-op: the reel
ships with exactly the caption/hashtags it would have shipped with before this feature existed.

**No new StageEvent stage name — reuses `"caption_hashtags"`, the structured path's existing stage
name.** Both paths are doing the conceptually identical operation (an LLM call converting real VO
content into a caption + hashtag set); giving them the same stage label keeps
`_pipeline_summary()`'s per-stage breakdown table meaningful without a redundant near-duplicate label.

**Cost/budget tradeoff, stated explicitly:** this adds one extra paid LLM call to every standard-path
reel that wasn't there before (the main single-shot generation call already produced *some*
caption/hashtags "for free," as part of the same completion). This is the same tradeoff `hook_
variants` already made for the identical reason (a materially better-targeted output is worth one
more budget-gated call), and is bounded by the existing `max_paid_llm_calls_per_reel` cap — never an
unbounded cost increase.

## 4. Implementation

`worker/tasks/generate.py`:

```python
def _generate_caption_hashtags(
    vo_scripts: list[str], niche: str, llm
) -> tuple[str, list[str]]:
    combined_vo = " ".join(v for v in vo_scripts if v)[:600]
    ...  # unchanged below this line
```

Structured path's one call site:

```python
caption, hashtags = _generate_caption_hashtags(
    [s.vo_script for s in stubs], niche, enrichment_llm
)
```

Standard path, appended inside the `if guide is None:` block, after the best-of-N acceptance:

```python
if guide is not None and guide.cuts \
        and paid_call_count(db, reel.id) < settings.max_paid_llm_calls_per_reel:
    caption_provider = "nvidia" if settings.nvidia_api_key else "ollama"
    caption_llm = get_enrichment_provider()
    vo_scripts = [b.vo_script for b in guide.cuts[0].beats]
    with record_stage(db, reel.id, "caption_hashtags", provider=caption_provider) as ev:
        new_caption, new_hashtags = _generate_caption_hashtags(
            vo_scripts, reel.niche or "general", caption_llm,
        )
        ev.detail["replaced"] = bool(new_caption and new_hashtags)
        # ... usage/cost tracking, same shape as every other record_stage call site
    if new_caption and new_hashtags:
        for platform_guide in guide.cuts:
            platform_guide.caption = new_caption
            platform_guide.hashtags = new_hashtags
```

## 5. Failure strategy

`_generate_caption_hashtags()` already has the correct internal contract (re-raise
`SoftTimeLimitExceeded`, degrade to `("", [])` on any ordinary error) — this design adds no new
try/except around the call itself, matching how the structured path already calls it directly. The
new `if new_caption and new_hashtags:` guard is the only new failure-handling logic, and its effect
is a genuine no-op on failure (see §3).

## 6. Test plan

`tests/test_generate_task.py`:

1. `test_standard_path_regenerates_caption_hashtags_from_real_vo_content` — `_generate_caption_
   hashtags` mocked to return distinct, clearly-not-generic content; asserts it's called with the
   accepted guide's real `vo_script` text (not empty/placeholder), and that every cut's `caption`/
   `hashtags` reflects the new values, not `_valid_guide_raw()`'s original single-shot values.
2. `test_standard_path_keeps_original_caption_when_regeneration_fails` — `_generate_caption_
   hashtags` mocked to its own documented failure return `("", [])`; asserts the cut's caption/
   hashtags are exactly `_valid_guide_raw()`'s original values, unchanged.
3. Existing standard-path-success tests (`test_seeded_performance_notes_survive_past_attempt_1_on_
   retry`, `test_genuine_structured_fallback_still_recorded_after_the_strip`) needed
   `_generate_caption_hashtags` patched to `("", [])` to stay fully mocked/deterministic — without
   it, they would make a real (unmocked) `get_enrichment_provider()` → `llm.complete()` call; in this
   sandboxed environment that fails fast (no local Ollama reachable) and degrades harmlessly, but
   relying on "no LLM server is reachable" for a fast, deterministic test is not a property this
   suite should depend on — an environment WITH a reachable Ollama/NVIDIA endpoint would make these
   tests slow or non-deterministic. Fixed by explicit mocking, matching this file's existing
   convention of patching every side-effecting call.
4. A dedicated budget-exhaustion test (mirroring `generate_hook_variants`' own identical
   `paid_call_count(...) < settings.max_paid_llm_calls_per_reel` gate) was considered and
   deliberately not added: `_enforce_paid_call_budget()` already raises at task entry and before
   every standard-path attempt whenever the budget is exhausted, so isolating "budget exhausted
   specifically by the time this NEW gate runs, but not before" requires a call-order-dependent
   `paid_call_count` side-effect sequence — fragile, and this exact gate shape (`generate_hook_
   variants`' own budget check) has no dedicated test in this codebase today either. Not adding one
   here is scope-matched to the existing precedent, not an oversight.

Both new tests mutation-tested against a reverted version of the fix before considering this task
done (see §7).

## 7. Mutation testing

- Removed the entire standard-path caption-regeneration block: `test_standard_path_regenerates_
  caption_hashtags_from_real_vo_content` failed as expected (`_generate_caption_hashtags` never
  called). `test_standard_path_keeps_original_caption_when_regeneration_fails` still passed against
  the full revert — expected and correct, since that test's property ("the original caption survives
  when regeneration fails") is trivially true when the feature doesn't exist at all; it isn't meant
  to prove the feature's existence, only its failure-path safety.
- Restored the feature, then mutated only the `if new_caption and new_hashtags:` guard to
  unconditionally overwrite regardless of success/failure: `test_standard_path_keeps_original_
  caption_when_regeneration_fails` failed as expected (`cut.guide["caption"] == ""` instead of the
  original value) — this is the test that actually proves the failure-path guard does its job.
- Both mutations restored; full suite (786 tests) and `ruff check --select F,E9 .` green afterward.

## 8. Corrections — review on the opened PR

A deep 4-persona review (Security/Red-Team, Correctness/Edge-Case, Test-Quality Auditor,
Documentation-Consistency, this pipeline's default review depth) ran against the opened PR.
Security/Red-Team and Documentation-Consistency found nothing (the latter flagged one pre-existing,
unrelated stale test count in `docs/architecture.md`, fixed opportunistically). Correctness/Edge-Case
and Test-Quality Auditor each found one real, non-blocking gap:

1. **Wasted call in `music_only`/`silent` voiceover_mode (Correctness/Edge-Case)** —
   `build_messages()`'s own prompt instructs the LLM to leave every beat's `vo_script` empty in
   these modes. The new caption-regeneration call would still fire with nothing to ground a caption
   in — `_generate_caption_hashtags()`'s own validation would reject the result and the guide's
   original caption/hashtags would survive untouched either way, so this was never a correctness bug,
   only a wasted paid call and a small latency cost on a mode this fix's whole premise doesn't apply
   to. Fixed by gating the call on `any(v.strip() for v in vo_scripts)` — a new, dedicated test
   (`test_standard_path_skips_caption_regeneration_when_every_vo_script_is_empty`) proves the call is
   skipped outright, mutation-tested by removing the guard and confirming the test then fails (the
   mocked `_generate_caption_hashtags` gets called with no `return_value` configured, itself
   surfacing as an unpack error — proof the call fired when it shouldn't have).
2. **Multi-platform overwrite never exercised (Test-Quality Auditor)** — every existing test used a
   single-cut/single-platform fixture (`_cut()`, `_valid_guide_raw()`), so a bug that only overwrote
   `guide.cuts[0]`'s caption/hashtags instead of looping every `platform_guide` would have passed
   every existing test undetected. Fixed with a new two-platform test
   (`test_standard_path_regenerates_caption_hashtags_for_every_platform_not_just_the_first`,
   `youtube_shorts` + `instagram_reels`, each with its own distinct original caption), asserting both
   platforms' `cut.caption`/`cut.hashtags` are overwritten — mutation-tested by changing the
   production overwrite loop to touch only `guide.cuts[0]` and confirming the test fails specifically
   on the second platform's cut.

Both fixes applied and mutation-tested before merge; full suite (788 tests) and
`ruff check --select F,E9 .` green afterward.
