# Module design: `worker/tasks/generate.py::generate_guide()` decomposition (CAR-1)

**Status:** Implemented, revised once after a deep 4-persona review on the opened PR found one real
regression (see §5). **Origin:** CAR-1, a "Worth exploring" candidate from a full-repository
codebase architecture review (report-only, not persisted to this repo). **Scope:** intended as a
pure internal refactor of `generate_guide()` — no behavior change — and verified as such by running
the full pre-existing test suite against the decomposed code before touching a single test, but the
opened-PR review found the first version was NOT in fact behavior-preserving; see §5 for the full
account.

## 1. Problem

`generate_guide()` was a single 262-line function concentrating nine responsibilities: stale
`job.meta`-key stripping, active `PerformanceNote` query, generation-path resolution (structured vs.
standard), structured-path execution + quality-gate fallback decision, the standard-path 3-attempt
retry loop with per-attempt paid-call budget enforcement, best-effort caption/hashtags regeneration
(Phase 7o), best-effort hook-variant generation, per-cut persistence, and final `job.meta`
finalization. 13 of `tests/test_generate_task.py`'s 17 tests drove the whole function end-to-end
(the other 4 were already isolated unit tests of pre-existing helper functions, unaffected by this
decomposition), each needing 6–10 `patch(...)` calls across unrelated dependencies to isolate one
behavior. Two
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
   Of the remaining 11 tests, 7 stay end-to-end tests of `generate_guide` itself — they exercise
   genuinely cross-cutting orchestration (`_prepare_generate`'s guards, path resolution, `job.meta`
   ownership/ordering across a structured→standard fallback) that no single extracted function
   owns, matching the module design's own honest assessment that not every test could narrow — and
   the other 4 are the pre-existing already-isolated helper tests from §1, unaffected either way.
3. **Mutation-tested two of the narrowed regression tests** against the same bug classes their
   originals were written to catch: (a) the multi-platform overwrite test, mutated to only touch
   `guide.cuts[0]`, fails with the exact expected assertion; (b) the seeded-performance-notes test,
   mutated to a plain-replace `feedback = [...]` (the original retry-replace bug), fails with the
   exact expected assertion. Both restored.
4. Full suite (792 tests at this point — 17 tests in this file, unchanged count from `main`'s own
   17 — before the 3 further tests §5 adds; a draft of this section originally miscounted this as
   788, caught and corrected by the opened-PR review) green; `ruff check --select F,E9 .` clean.

## 5. Corrections — deep 4-persona review on the opened PR

A deep 4-persona review (Security/Red-Team, Correctness/Edge-Case, Test-Quality Auditor,
Documentation-Consistency, this pipeline's default review depth per session convention) ran against
the opened PR. Security/Red-Team found nothing. The other three found:

**One real, empirically-confirmed regression (Correctness/Edge-Case) — the "pure refactor, no
behavior change" claim in §4 above was false for the first version of this decomposition.**
The reviewer ran the SAME scenario (structured path succeeds with score 90, threshold 65) against
both `main` (old code) and this branch (new code), with `_generate_caption_hashtags` spied via
`patch`, and observed a real divergence: `caption_hashtags called: False` on `main`,
`caption_hashtags called: True` on the new code. Root cause: the original inline caption/hashtags-
regeneration block sat at 8-space indent, nested *inside* `if guide is None:` (the standard-path-
only block) — a direct structured-path success never reached it, since
`_generate_from_structured_script()` already produces content-aware caption/hashtags via its own
`_generate_caption_hashtags()` call. The first decomposed version called
`_maybe_regenerate_caption_hashtags(ctx, guide)` unconditionally from the orchestrator, after either
path-runner produced a guide, with no branch distinguishing which path won. Impact for every reel
taking the (fast, recommended) structured path and succeeding on the first try: one extra,
previously-nonexistent paid `caption_hashtags` LLM call + `StageEvent`, consuming one more slot of
`max_paid_llm_calls_per_reel`, that could silently overwrite the structured path's own
already-content-aware caption/hashtags with a second, independently-sampled (nondeterministic) LLM
result — and, concretely, caused two pre-existing tests
(`test_structured_path_success_does_not_nameerror_on_performance_notes`,
`test_stale_structured_fallback_does_not_leak_into_a_clean_success`) to silently attempt a real,
unmocked LLM-provider construction inside what should have been a fully-mocked unit test (masked
only by `_generate_caption_hashtags`'s own `except Exception: pass` swallowing the resulting
connection failure). §4's own "all 17 tests pass unmodified" verification step did not catch this,
because no original test used a spy/mock on `_generate_caption_hashtags` while exercising the
structured-success path — only `NameError`-absence and `job.meta` content were asserted, neither of
which distinguishes zero calls from one extra call. Fixed by moving the
`_maybe_regenerate_caption_hashtags(ctx, guide)` call from the orchestrator into
`_run_standard_path_attempts()` itself, right before it returns — restoring the original
standard-path-only ownership exactly, not merely patching the symptom. Hook-variant generation was
NOT affected: it was already guide-agnostic in the original inline code (running unconditionally
after either path produced a guide, outside the `if guide is None:` block), so it correctly stayed
in the orchestrator. A new regression test
(`test_structured_path_success_does_not_regenerate_caption_hashtags_a_second_time`) asserts
`_generate_caption_hashtags` is never called on a structured-path success — mutation-tested by
reverting the fix and confirming the test fails for the exact right reason.

**Two real test-coverage gaps (Test-Quality Auditor).** (1) `_persist_guide()` had zero direct test
coverage — the 4 narrowed caption-regeneration tests assert only on the pydantic `guide` object,
never on the `Cut` ORM rows `_persist_guide()` actually writes; a wrong platform-matching bug, a
swapped field, or a dropped `hook_variants` assignment would have passed every test in this file
undetected. Closed with `test_persist_guide_writes_every_matching_cut_and_skips_unmatched_
platforms` — a **two-platform, reversed-cut-order** fixture was required, not a single-platform one:
a single-platform version of this test was tried first and found to pass even against the exact
"always use the first cut regardless of platform" mutation, since `cuts[0]` coincidentally matched
the only platform_guide present by luck. (2) The narrowed
`test_seeded_performance_notes_survive_past_attempt_1_on_retry` now injects `active_notes` as plain
strings directly into `_GenerationContext`, bypassing `generate_guide()`'s own `active_notes = [n.text
for n in active_notes_rows]` extraction entirely — a regression swapping `.text` for `.id` (or any
other field) there would ship silently, with no crash and no failing assertion anywhere in the
suite. Closed with `test_active_performance_notes_text_is_correctly_extracted_and_seeded`, a real
end-to-end run with a real `PerformanceNote`-shaped row, mutation-tested by swapping `.text` for
`.id` in the extraction and confirming the test fails.

**Documentation-Consistency found three real count discrepancies**, all in this document's and
CLAUDE.md's own prose (not in the code): "15 of 17 tests drove the whole thing end-to-end" was wrong
(actual pre-refactor count was 13 — 4 tests were always isolated helper-function tests, never
end-to-end); "the other 11 stay end-to-end tests of `generate_guide` itself" was wrong (only 7 of
those 11 actually call `generate_guide` — the other 4 are the same pre-existing isolated tests from
the first miscount); and "788 tests, unchanged count" was wrong (the real pre-this-PR baseline,
verified against `main` in a throwaway worktree, was 792 total / 791 default-run + 1 deselected).
All three corrected throughout this document and CLAUDE.md.

All fixes applied and mutation-tested before merge; full suite (795 tests — 792 + the 3 tests added
in this round) green, `ruff check --select F,E9 .` clean.

The full `MODULE_DESIGN_SPEC.md` this implementation followed (evidence ledger, depth assessment,
contract tables, dependency-direction table) was produced by the `module-design` skill and sent
directly to the user rather than persisted as a separate file — this document is its implementation
record.
