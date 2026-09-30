# Module design: `worker/tasks/generate.py::generate_guide()` decomposition (CAR-1)

**Status:** Implemented. **Origin:** CAR-1, a "Worth exploring" candidate from a full-repository
codebase architecture review (report-only, not persisted to this repo). **Scope:** pure internal
refactor of `generate_guide()` — no behavior change; verified by running the full pre-existing test
suite against the decomposed code before touching a single test.

## 1. Problem

`generate_guide()` was a single 262-line function concentrating nine responsibilities: stale
`job.meta`-key stripping, active `PerformanceNote` query, generation-path resolution (structured vs.
standard), structured-path execution + quality-gate fallback decision, the standard-path 3-attempt
retry loop with per-attempt paid-call budget enforcement, best-effort caption/hashtags regeneration
(Phase 7o), best-effort hook-variant generation, per-cut persistence, and final `job.meta`
finalization. 15 of `tests/test_generate_task.py`'s 17 tests drove the whole function end-to-end,
each needing 6–10 `patch(...)` calls across unrelated dependencies to isolate one behavior. Two
~20-line blocks (caption/hashtags regeneration, hook-variant generation) independently implemented
an identical 5-step shape: precondition → paid-call budget gate → provider selection →
`get_enrichment_provider()` → `record_stage(...)` + usage/token/cost bookkeeping.

Falsification (done at the architecture-review stage, repeated here): zero external in-process
callers reach `generate_guide`'s internals — the only references outside this file are 3 opaque
task-boundary uses (`enrich_context.py`'s `.delay()`, `maintenance.py`'s resume registry,
`celery_app.py`'s routing config). Migration risk for internal decomposition was assessed as low for
exactly this reason, and confirmed empirically below.

## 2. Design

Extracted, in reading order:

- `_strip_stale_fallback_meta(db, job)` — the Phase 7f stale-meta strip, unchanged logic.
- `_load_active_performance_notes(db)` — the unconditional `PerformanceNote` query.
- `_GenerationContext` (dataclass) — bundles the 13 read-only values every section needs
  (`db, job, reel, cuts, platforms, target_lengths, max_target, voiceover_mode, effective_context,
  active_notes, axis_multipliers, quality_threshold, llm`), constructed once in `generate_guide`.
  **Chosen over threading the 13 values individually** (Design B, rejected) — that alternative
  directly reproduces the exact unbundled-parameter-list risk the decomposition exists to avoid; see
  the design's own Rejected-alternatives reasoning below.
- `GuideResult` (`NamedTuple`: `guide, score, issues, exc`) — the common return shape for both
  path-runners.
- `_try_structured_path(ctx, stubs) -> GuideResult` — the structured-path try/except, including the
  quality-gate fallback decision and its own `job.meta["path"]="structured"` /
  `["structured_score"]` / `["structured_fallback"]` writes. Re-raises `SoftTimeLimitExceeded`
  uncaught, unchanged.
- `_run_standard_path_attempts(ctx, stubs) -> GuideResult` — the 3-attempt retry loop, best-of-N
  acceptance, and its own `job.meta["path"]="standard"` write. `feedback` accumulation stays
  additive (`active_notes + issues`), unchanged — this is the exact property
  `test_seeded_performance_notes_survive_past_attempt_1_on_retry` guards.
- `_best_effort_llm_call(db, reel_id, stage, call, detail=None) -> T | None` — the one earned shared
  seam: the identical budget-gate/provider-select/`record_stage` shape both best-effort calls
  duplicated. Takes a `call: Callable[[llm], T]` and an optional `detail: Callable[[T], dict]` to
  preserve each call site's own `ev.detail` diagnostic key (`"replaced"` / `"variant_count"`) from
  inside the same `record_stage` transaction.
- `_maybe_regenerate_caption_hashtags(ctx, guide) -> None` and
  `_maybe_generate_hook_variants(ctx, guide) -> list[str]` — each keeps its own precondition and
  result-application logic; **not** merged into one function (see Rejected alternatives).
- `_persist_guide(cuts, guide, hook_variants) -> None` — the final per-cut write loop.

`generate_guide` itself is now a short orchestration sequence: build `ctx` → try structured, fall
back to standard → raise on total failure → run the two best-effort calls → persist → transition →
write the final `job.meta`.

## 3. Rejected alternatives

- **Design B — individually-threaded parameters instead of `_GenerationContext`.** Real advantages
  exist (no new type to learn; each function's true dependency set stays fully visible at its call
  site; zero risk of the context object silently accumulating unrelated fields over time) but were
  outweighed by reproducing the exact 8+-parameter risk the decomposition exists to fix — a
  12-parameter `_run_standard_path_attempts` signature is not meaningfully more local than reading
  the same values off `generate_guide`'s own body.
- **A single merged `_maybe_best_effort_call(kind, ...)`.** The two calls differ in precondition
  shape (list-of-strings vs. single-beat), return type (`tuple[str, list[str]]` vs. `list[str]`),
  and result-application (loop over every `platform_guide` vs. one local variable). Merging them
  would either erase type information behind an internal `kind`-branch or force a
  lowest-common-denominator signature — a seam that does not earn its cost, per the shared codebase
  design doctrine's warning against forcing near-duplicate-but-different code into one abstraction.

## 4. Verification

1. **Behavior preservation, empirically proven, not just argued.** All 17 pre-existing tests in
   `tests/test_generate_task.py` pass **unmodified** against the decomposed code, before any test
   was touched — the strongest available evidence this refactor changed no observable behavior.
2. **Test narrowing, per the module design's own test-surface analysis.** 6 of the 17 tests were
   then rewritten to call the newly-extracted functions directly instead of driving the whole task:
   - `test_the_soft_time_limit_in_the_structured_path_does_not_fall_back_to_the_standard_path` →
     direct test of `_try_structured_path`.
   - `test_standard_path_regenerates_caption_hashtags_from_real_vo_content`,
     `test_standard_path_regenerates_caption_hashtags_for_every_platform_not_just_the_first`,
     `test_standard_path_skips_caption_regeneration_when_every_vo_script_is_empty`,
     `test_standard_path_keeps_original_caption_when_regeneration_fails` → direct tests of
     `_maybe_regenerate_caption_hashtags`, no longer needing the LLM provider, `build_messages`,
     `score_guide`, `_combined_score`, `_enrich_standard_path_guide`, `generate_hook_variants`, or
     `SessionLocal`/`job_task`'s own lifecycle at all — the guide is constructed directly instead of
     round-tripped through a mocked LLM completion.
   - `test_seeded_performance_notes_survive_past_attempt_1_on_retry` → direct test of
     `_run_standard_path_attempts`, dropping path-selection and best-effort-call mocking.
   The remaining 11 tests stay end-to-end tests of `generate_guide` itself — they exercise genuinely
   cross-cutting orchestration (`_prepare_generate`'s guards, path resolution, `job.meta`
   ownership/ordering across a structured→standard fallback) that no single extracted function
   owns, matching the module design's own honest assessment that not every test could narrow.
3. **Mutation-tested two of the narrowed regression tests** against the same bug classes their
   originals were written to catch: (a) the multi-platform overwrite test, mutated to only touch
   `guide.cuts[0]`, fails with the exact expected assertion; (b) the seeded-performance-notes test,
   mutated to a plain-replace `feedback = [...]` (the original retry-replace bug), fails with the
   exact expected assertion. Both restored.
4. Full suite (788 tests, unchanged count — a 1:1 test rewrite, not an addition) green;
   `ruff check --select F,E9 .` clean.

The full `MODULE_DESIGN_SPEC.md` this implementation followed (evidence ledger, depth assessment,
contract tables, dependency-direction table) was produced by the `module-design` skill and sent
directly to the user rather than persisted as a separate file — this document is its implementation
record.
