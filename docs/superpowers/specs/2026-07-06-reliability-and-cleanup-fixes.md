# Reliability, Arch Cleanup, and Doc-Drift Fixes — Design Spec

> **⚠️ SUPERSEDED (2026-08-02)** — do not implement. Fixes 1 and 5 have shipped; Fix 2 does not
> work as written (the proposed retry hits the task's own idempotency guard and silently no-ops).
> The remaining work is re-scoped in
> [`2026-08-02-retry-and-task-cleanup-design.md`](2026-08-02-retry-and-task-cleanup-design.md),
> which shipped in full. Kept for history only.

**Date:** 2026-07-06

---

## Problem statement

A full-codebase audit surfaced four categories of issues:

1. **Correctness bugs** — a stuck-job reaper gap that leaves `enriching` reels permanently unrecoverable; dead retry config on two Celery tasks; unguarded `db.get()` calls that turn a deleted reel/cut into a raw `AttributeError` instead of a clean job failure.
2. **Architecture smell** — `worker/tasks/generate.py` (653 lines) mixes task orchestration with content-generation helpers (visual fallback tables, name extraction, enrichment prompts) that belong in `engine/generation/`. `_heartbeat()` is duplicated verbatim across three task files.
3. **Doc drift** — `CLAUDE.md` understates the test suite (68/5 files vs actual 97/7), understates migrations (says "0001 + 0002", a third migration exists), and omits the `enrich_context` pipeline stage entirely from the Architecture section.
4. **Test coverage gaps** — several fragile/important code paths (reaper's `enriching` revert, `resolve_or_reuse` re-pin, `judge_guide` failure fallback, TTS rate-clamp math) have no direct test.

Credential key-rotation and product-gap items (publishing, music mixing) are explicitly out of scope for this pass — deferred, not blocking.

---

## Approach

Straight surgical fixes, no new dependencies, no schema changes. Grouped into four fix sets below. Each is independently testable and independently committable, but shipped together since they're small and related (all came out of the same audit pass).

---

## Fix 1 — Reaper misses `enriching` reels

**File:** `worker/tasks/maintenance.py`

**Root cause:** `_revert_owner()` (line 47) only reverts a reel when `reel.status.value == "generating"`. If the `enrich_context` job dies mid-run (worker OOM/kill), the reaper fails the `Job` row but the `Reel` stays `"enriching"` forever — `REEL_TRANSITIONS` has no incoming transition to reach it from anywhere except `"draft"`, so it's a dead end with no UI path to retry.

**Fix:** Widen the check to both owning statuses:

```python
if reel and reel.status.value in ("generating", "enriching"):
    try:
        transition(reel, "failed", REEL_TRANSITIONS)
    except ValueError:
        pass
```

`REEL_TRANSITIONS["enriching"]` already permits `→ failed`, so no state-machine change needed.

**Test:** new case in `tests/test_maintenance.py` (new file) — stuck `Job` with `reel.status = "enriching"` and stale heartbeat → after `reap_stuck_jobs()`, reel is `"failed"`.

---

## Fix 2 — Wire real retry for transient failures

**Files:** new `worker/tasks/common.py`; `worker/tasks/generate.py`; `worker/tasks/render.py`

**Root cause:** Both tasks declare `@celery_app.task(bind=True, max_retries=2)` but never call `self.retry()`. The param is pure noise — on any exception the task fails once, no different than `max_retries=0`.

**Fix:** Add a classifier for exceptions worth retrying (network/IO blips) vs. not (deterministic failures where retrying with the same input produces the same result):

```python
# worker/tasks/common.py
import subprocess
import httpx

def is_transient_error(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TransportError, TimeoutError, subprocess.TimeoutExpired, ConnectionError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in (429, 500, 502, 503, 504)
    return False
```

In each task's outer `except Exception as exc:` block, before marking the job/reel/cut failed:

```python
if is_transient_error(exc) and self.request.retries < self.max_retries:
    db.rollback()
    raise self.retry(exc=exc, countdown=30 * 2 ** self.request.retries)
```

This runs *before* the existing fail-and-transition logic, so deterministic failures (bad LLM JSON, quality-below-threshold `ValueError`, `ValidationError`) behave exactly as today — one attempt, clean failure, no wasted retries on something that won't change. `max_retries=2` becomes real: two retries at 30s/60s backoff for actual connection blips (LLM API down, Pexels/Wikipedia/HF timeout, transient 5xx).

Note: `enrich_context.py` already has `max_retries=0` (explicit, not accidental) — left unchanged.

**Test:** `tests/test_common.py` (new) — table-driven cases for `is_transient_error` (httpx timeout → True, httpx 404 → False, ValueError → False, httpx 503 → True).

---

## Fix 3 — Null-guard `db.get()` results

**Files:** `worker/tasks/generate.py` (line 482), `worker/tasks/render.py` (lines 52–53), `worker/tasks/enrich_context.py` (line 41)

**Root cause:** After the existing `if job is None: return` guard, each task does `reel = db.get(models.Reel, job.reel_id)` (or `cut = db.get(...)`) with no check. If the row was deleted between job creation and worker pickup, the next line that touches `reel.foo` throws `AttributeError`, which is caught by the outer `except Exception` — but the resulting `job.error` message is a raw Python attribute-error string, not something an operator can act on.

**Fix:** immediately after each `db.get()`, add:

```python
if reel is None:
    job.status = models.JobStatus.failed
    job.error = f"Reel {job.reel_id} no longer exists"
    db.commit()
    return
```

(mirrored for `cut` in `render.py`, using `job.cut_id`). No retry needed — this is not transient.

**Test:** one case per task in the relevant existing test file — delete the reel/cut before invoking the task, assert `job.status == failed` and `job.error` mentions the missing id, no unhandled exception raised.

---

## Fix 4 — Split `generate.py`

**File:** `worker/tasks/generate.py` → new `engine/generation/visual_fallback.py`, new `engine/generation/beat_enrichment.py`

**Root cause:** `generate.py` is the largest file in the repo (653 lines) and mixes two concerns that don't belong with Celery task orchestration: static visual-fallback data/lookup, and beat-level insight/conflict enrichment. Both are pure content-generation logic with no dependency on Celery, `Job`, or `db`.

**Move to `engine/generation/visual_fallback.py`** (drop leading underscore — becomes public module API):
- `_SECTION_FALLBACK_VISUALS` → `SECTION_FALLBACK_VISUALS`
- `_VO_TO_SHOT` → `VO_TO_SHOT`
- `_fallback_visual()` → `fallback_visual()`
- `_VO_NAME_RE`, `_NON_PERSON`, `_NON_PERSON_PREFIXES` → same names, no underscore
- `_first_person()` → `first_person()`
- `_DEGENERATE_SUFFIXES`, `_DEGENERATE_PHRASES` → same names, no underscore
- `_is_degenerate_visual()` → `is_degenerate_visual()`

**Move to `engine/generation/beat_enrichment.py`**:
- `_TACTICAL_MARKERS` → `TACTICAL_MARKERS`
- `_is_shallow_beat()` → `is_shallow_beat()`
- `_ENRICH_BATCH` → `ENRICH_BATCH`
- `_enrich_batch()` → `enrich_batch()`
- `_enrich_with_insight()` → `enrich_with_insight()`
- `_CONFLICT_RE` → `CONFLICT_RE`
- `_has_conflict_beat()` → `has_conflict_beat()`
- `_make_conflict_stub()` → `make_conflict_stub()`

**Stays in `generate.py`**: task orchestration (`generate_guide`), `_combined_score`, `_stubs_to_platform_guide`, `_generate_caption_hashtags`, `_generate_from_structured_script`, `_enrich_standard_path_guide` — these all touch `db`, `record_stage`, or `Job`/`reel` directly, or are orchestration glue. `generate.py` drops from 653 to roughly 350 lines.

**Also move** `_heartbeat()` out of `generate.py`, `render.py`, and `enrich_context.py` into `worker/tasks/common.py::heartbeat()` (Fix 2 already creates this file). All three tasks import it from there instead of redefining it.

**Fallout:** `tests/test_enrichment.py:126,138` import `_enrich_batch`/`_make_conflict_stub` from `worker.tasks.generate` — update to import `enrich_batch`/`make_conflict_stub` from `engine.generation.beat_enrichment`.

**Test:** no new tests needed — existing tests for these functions move with them (same assertions, new import path, new public names).

---

## Fix 5 — Doc drift in `CLAUDE.md`

**File:** `/Users/luckyratanlaljain/project/reel-maker/CLAUDE.md`

- Test count in the `Commands` section comment: `# 68 tests across 5 files` → `# 97 tests across 7 files` (matches the module-layout table, which already lists `test_state.py` and `test_context_enricher.py`).
- Migration comment: `# run migrations (0001 + 0002)` → `# run migrations (0001–0003)`.
- Architecture section: add the missing `enrich_context` stage. Current text implies `POST /api/reels` enqueues `generate_guide` directly; actual flow (per `api/routers/reels.py` + `worker/tasks/enrich_context.py`) is: create `Reel`+`Cut`+`Job` rows → enqueue `enrich_context` → it scores context quality, optionally enriches it (skipped for structured scripts), transitions reel to `generating`, creates+enqueues the `generate_guide` job. Add one paragraph describing this stage under "Guide generation" before the "two paths" breakdown.
- Also fold in the new `worker/tasks/common.py` and the two new `engine/generation/` modules into the "Module layout" tree, and note the retry/backoff behavior added in Fix 2 under "Reliability features".

No test needed (docs only).

---

## Fix 6 — Test-coverage backfill

Four previously-untested paths, one test each, added alongside the fixes above (some already covered by Fixes 1–3's own tests):

1. **Reaper `enriching` revert** — covered by Fix 1's test.
2. **`resolve_or_reuse` re-pin path** (`engine/render/asset_sourcer.py`) — new test in `tests/test_asset_sourcer.py` (new file, or existing if one is found during implementation): pin a `CutAsset` for beat 0, change `visual_direction`, call `resolve_or_reuse` again, assert the stale `CutAsset` row is replaced (not duplicated) and the new fingerprint is stored.
3. **`judge_guide` failure fallback** (`engine/generation/llm_judge.py`) — new case in `tests/test_llm_judge.py` (new file if none exists): LLM provider raises/returns garbage → `judge_guide` returns `(50, [...])` rather than propagating.
4. **`EdgeTTSProvider.synth_to_budget` rate-clamp math** (`engine/render/tts.py`) — new case in a TTS test file: duration far outside target triggers a re-synth request with `rate` clamped to ±25%, verify the clamp boundary (e.g. a target that would compute to +40% is clamped to +25%).

---

## Out of scope (explicitly deferred)

- Credential key-rotation / dual-key support in `crypto.py` — no OAuth flow exists yet (Phase 4 not started), nothing is bricked today.
- Whisper double-transcription cost in the compositor — noted as a latency observation, not a bug; no action this pass.
- Product gaps (publishing, music mixing, Chatterbox TTS, Pixabay) — separate, larger initiatives.

---

## Validation

- `.venv/bin/pytest` — full suite green, including new tests, before and after the `generate.py` split (split must not change any test's assertions, only import paths).
- Manual: kill a Celery worker mid-`enrich_context` run, confirm `reap_stuck_jobs` now fails the reel instead of leaving it stuck (or a targeted unit test standing in for this if manual repro is impractical).
