# Retry Semantics and Task-Module Cleanup — Design Spec

**Date:** 2026-08-02

**Supersedes:** `docs/superpowers/specs/2026-07-06-reliability-and-cleanup-fixes.md` and its plan
`docs/superpowers/plans/2026-07-07-reliability-and-cleanup-fixes.md`. Those two documents remain in
git as history. Do not implement them; three of their six fixes have already landed, and one of the
remaining three (Fix 2) does not work as written. This spec covers only what is left, re-grounded in
the code as it stands after commits `905af21` and `b7e444c`.

---

## What already landed

A full-repo review on 2026-08-02 shipped part of the older spec plus several issues it never covered.

| Old spec item | Status |
|---|---|
| Fix 1 — reaper misses `enriching` reels | Done. Also added a `pending`-job reap the old spec did not anticipate. |
| Fix 2 — wire real retry | **Not done, and the proposed design is broken** (see below). |
| Fix 3 — null-guard `db.get()` | Not done. |
| Fix 4 — split `generate.py` | Not done. `generate.py` is 652 lines; `_heartbeat` is still defined in three task files. |
| Fix 5 — doc drift | Mostly done (test counts, migration range, module layout, queue list). One item outstanding: the Architecture section still omits the `enrich_context` stage. |
| Fix 6 — test backfill | 1 of 4 done (reaper). `resolve_or_reuse` re-pin, `judge_guide` fallback, and `synth_to_budget` clamp remain untested. |

The old spec's "out of scope" list is also stale: it deferred the compositor's Whisper transcription
cost as "a latency observation, not a bug". That was fixed (`@lru_cache` on the model load) along with
a caption-timing bug in the same function.

---

## Problem statement

Three things remain, in descending order of consequence.

1. **`max_retries=2` is dead config on `generate_guide` and `render_cut`.** Neither task calls
   `self.retry()`, so a transient LLM or IO failure fails the job permanently on first contact —
   identical behaviour to `max_retries=0`, but reading as though retry exists.

2. **Unguarded `db.get()` results.** After the `if job is None: return` guard, each task fetches its
   reel/cut without a null check (`generate.py:482`, `render.py:33-34`, `enrich_context.py:34`). A row
   deleted between job creation and worker pickup produces a raw `AttributeError` in `job.error`
   instead of something an operator can act on. `render.py` is the worst of the three: it does
   `cut = db.get(...)` then immediately `db.get(models.Reel, cut.reel_id)`.

3. **`worker/tasks/generate.py` mixes orchestration with content generation.** 652 lines, of which
   the visual-fallback tables and the beat-enrichment prompts have no dependency on Celery, `Job`, or
   `db`. `_heartbeat()` is duplicated verbatim in three task files.

Plus one carried-over doc gap and three untested paths, listed under Shipment 1 below.

---

## Why the old Fix 2 does not work

The old spec proposed, inside each task's outer exception handler:

```python
if is_transient_error(exc) and self.request.retries < self.max_retries:
    db.rollback()
    raise self.retry(exc=exc, countdown=30 * 2 ** self.request.retries)
```

Both tasks commit `job.status = running` at entry (`generate.py:485-490`, `render.py:36-41`).
`db.rollback()` cannot undo an already-committed value, so the redelivered task hits its own
idempotency guard —

```python
if job.status in (models.JobStatus.done, models.JobStatus.running):
    return
```

— and returns immediately. **Every retry would be a silent no-op.** The job would then sit `running`
with a frozen heartbeat until the reaper failed it five minutes later, which is strictly worse than
today's fast clean failure. Retry and the idempotency guard were designed independently and collide.

---

## Approach

Two independently shippable changes, in this order. No new dependencies, no schema change, no
Alembic migration.

**Shipment 1 — failure handling.** Behaviour changes only; no files move.

**Shipment 2 — module split.** File moves only; no behaviour change.

The ordering is the point. A moved-file diff and a retry-semantics diff in one commit make a red
suite ambiguous. Split second, and any breakage can only be an import path — revertable without
giving back the correctness fixes. If appetite runs out, Shipment 2 is the piece that can be dropped
with no loss of correctness.

---

# Shipment 1 — failure handling

## 1.1 New module: `worker/tasks/common.py`

```python
import subprocess

import httpx

_TRANSIENT_TYPES = (
    httpx.TransportError,      # connect/read/write timeouts, connection errors
    TimeoutError,
    subprocess.TimeoutExpired,
    ConnectionError,
)
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def is_transient_error(exc: BaseException) -> bool:
    """True when re-running the same input could plausibly succeed."""
    if isinstance(exc, _TRANSIENT_TYPES):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS
    return False


def should_retry(exc: BaseException, retries: int, max_retries: int) -> bool:
    """Retry decision, split out from the Celery glue so it is unit-testable."""
    return retries < max_retries and is_transient_error(exc)
```

`heartbeat()` moves into this module in Shipment 2, not here — Shipment 1 must not touch the three
task files beyond their exception handlers and `db.get()` sites.

## 1.2 Retry branch

First statement inside the existing `except Exception as exc:` block in `generate_guide` and
`render_cut`, ahead of the fail-and-transition logic:

```python
if should_retry(exc, self.request.retries, self.max_retries):
    db.rollback()
    job = db.get(models.Job, job_id)
    if job:
        job.status = models.JobStatus.pending
        job.error = f"transient failure, retry {self.request.retries + 1}: {exc}"[:2000]
        db.commit()
    raise self.retry(exc=exc, countdown=30 * 2 ** self.request.retries)
```

Resetting to `pending` is what makes the retry real: the guard stays a plain check on DB state, and
`pending` is an honest description of a job waiting out a backoff. `self.retry()` raises `Retry`, so
the fail-and-transition code below is skipped; `finally: db.close()` still runs.

**Do not bump `job.attempts` here.** Both tasks already increment it at entry, and a retry re-enters
at entry — incrementing in both places double-counts every retry.

Deterministic failures are unaffected: `ValidationError` from a malformed guide, the
quality-below-threshold `ValueError`, the new missing-row `ValueError`, and ffmpeg's non-zero-exit
`RuntimeError` all fail once, exactly as today.

`enrich_context` keeps `max_retries=0`. That is deliberate, not an oversight: its only network call
(`llm_enrich`) already degrades to `None` on failure rather than raising, and the job is cheap to
re-run from the UI.

## 1.3 `render_cut` must clear `job.error` on success

`generate_guide` sets `job.error = None` on its success path; `render_cut` does not.
`ui/templates/fragments/render_status.html` renders `{% if job.error %}` regardless of job status, so
a render that fails transiently, retries, and succeeds would display the finished video with a red
"transient failure, retry 1: …" underneath it. Writing `job.error` during backoff turns this latent
bug into a visible one, so it is fixed in the same shipment:

```python
job.status = models.JobStatus.done
job.error = None          # <- add
```

## 1.4 Reaper: pending branch must key on `updated_at`

`reap_stuck_jobs` currently selects pending jobs by `created_at < now - 30min`. A long render created
40 minutes ago that fails transiently returns to `pending` and would be reaped mid-backoff, on the
next 60-second tick. Change the pending branch to `updated_at` — "pending and untouched for 30
minutes" — which is the condition actually intended. `Job.updated_at` already has `onupdate=_now`, so
the retry branch's commit refreshes it for free. The `running` branch is unchanged.

## 1.5 Null-guard the row lookups

At `generate.py:482`, `render.py:33-34`, and `enrich_context.py:34`:

```python
reel = db.get(models.Reel, job.reel_id)
if reel is None:
    raise ValueError(f"Reel {job.reel_id} no longer exists")
```

and in `render_cut`, the same for `cut` before `cut.reel_id` is dereferenced.

Raising into the existing handler rather than adding an explicit fail-and-return block keeps one
failure path instead of two. The handler already sets `status = failed` and writes `str(exc)` to
`job.error`, its `transition()` calls are already guarded on `if reel and ...` / `if cut and ...`, and
`ValueError` is not transient so it will not retry.

## 1.6 Doc gap carried over

`CLAUDE.md`'s Architecture section still describes `POST /api/reels` as enqueuing guide generation
directly. Add one paragraph for the real flow: create `Reel` + `Cut` + `Job` rows → enqueue
`enrich_context` → score context quality, optionally enrich (skipped for structured scripts) →
transition reel to `generating` → create and enqueue the `generate_guide` job. Also add the new
retry/backoff behaviour under "Reliability features" and `worker/tasks/common.py` to the module
layout.

## 1.7 Tests

Retry decisions live in `should_retry`, so the pure logic is tested directly and the task-level tests
only assert that the branch fires. Task tests follow the existing style — `patch("worker.tasks.X.SessionLocal")`,
call the task directly — with `patch.object(generate_guide, "retry", side_effect=Retry())` so no
broker is engaged.

| File | Cases |
|---|---|
| `tests/test_common.py` (new) | `is_transient_error`: httpx timeout → True, httpx 503 → True, httpx 404 → False, `subprocess.TimeoutExpired` → True, `ValueError` → False. `should_retry`: False at `retries == max_retries` even for a transient exception. |
| `tests/test_generate_task.py` (new) | Missing reel → job `failed`, message names the id, no unhandled exception. Transient exception → `retry` called and status reset to `pending`. Deterministic exception → no retry, job `failed`. |
| `tests/test_render_task.py` (new) | Missing cut → job `failed` with the id in the message. Success path leaves `job.error is None`. |
| `tests/test_enrich_context_task.py` | Missing reel → job `failed`. |
| `tests/test_maintenance.py` | Pending job with stale `created_at` but fresh `updated_at` is not reaped. |
| `tests/test_asset_sourcer.py` (new) | `resolve_or_reuse` re-pin, against a real in-memory SQLite session: pin a `CutAsset` for beat 0, change `visual_direction`, call again, assert the stale row is replaced (not duplicated) and the new fingerprint is stored. |
| `tests/test_llm_judge.py` (new) | `judge_guide` returns `(50, [...])` when the provider raises, and when it returns unparseable JSON. |
| `tests/test_tts.py` | `synth_to_budget`: a drift computing to +40% is requested as `+25%` (clamp boundary). |

---

# Shipment 2 — module split

## 2.1 Moves

Symbols move **verbatim, underscores intact**. The older spec's rename-to-public table is dropped:
these are internal helpers, not a published interface, and renaming them touches every call site and
every test import for no behavioural gain. Keeping the names means existing tests need a changed
import path and nothing else — which is the whole safety argument for the refactor. If any assertion
or symbol name changes, this stopped being a refactor.

**To `engine/generation/visual_fallback.py`:** `_SECTION_FALLBACK_VISUALS`, `_VO_TO_SHOT`,
`_fallback_visual()`, `_VO_NAME_RE`, `_NON_PERSON`, `_NON_PERSON_PREFIXES`, `_first_person()`,
`_DEGENERATE_SUFFIXES`, `_DEGENERATE_PHRASES`, `_is_degenerate_visual()`.

**To `engine/generation/beat_enrichment.py`:** `_TACTICAL_MARKERS`, `_is_shallow_beat()`,
`_ENRICH_BATCH`, `_enrich_batch()`, `_enrich_with_insight()`, `_CONFLICT_RE`, `_has_conflict_beat()`,
`_make_conflict_stub()`.

**To `worker/tasks/common.py`:** `_heartbeat()` → `heartbeat()`, deleted from `generate.py`,
`render.py`, and `enrich_context.py`. This one is renamed because it genuinely becomes a
cross-module API and the three copies must not diverge again.

**Stays in `generate.py`:** `generate_guide`, `_combined_score`, `_stubs_to_platform_guide`,
`_generate_caption_hashtags`, `_generate_from_structured_script`, `_enrich_standard_path_guide` —
each touches `db`, `record_stage`, or `Job`/`reel`. Result: roughly 350 lines.

## 2.2 Fallout

`tests/test_enrichment.py` imports `_enrich_batch` and `_make_conflict_stub` from
`worker.tasks.generate`; repoint to `engine.generation.beat_enrichment`. No new tests.

Update the `CLAUDE.md` module-layout tree with both new modules.

---

## Scope limits, stated honestly

**The retry branch buys less than "retry transient network failures" implies.**
`engine/render/asset_sourcer.py` catches every exception internally and returns `None` — a Pexels or
Wikipedia outage degrades to black frames rather than raising, so it never reaches the retry branch.
`judge_guide` and `_enrich_batch` likewise catch and return neutral values. The calls that actually
reach it are `llm.complete()` in `generate_guide`, ffprobe timeouts, and MoviePy/ffmpeg IO errors in
`render_cut`. Making the sourcer's silent degradation loud is a real question, and a larger one:
out of scope here.

**Nothing in this spec is verified against real infrastructure.** Every test is a unit test with a
mocked session or in-memory SQLite. The old spec's "manually kill a Celery worker mid-run" step is
dropped — the reaper tests assert the same state transition deterministically, and the manual repro
needs Postgres, Redis, and a live worker to demonstrate what a mock asserts in milliseconds. A real
end-to-end run is still worth doing before trusting any of this in production, together with the
render-path fixes from `b7e444c`.

## Out of scope

- `record_stage()` committing the caller's session. Every current call site sits on a commit
  boundary, so it is benign today; documented in `engine/observability.py` rather than refactored.
- Video readers opened inside `compositor._build_media_sub_clip`, still reclaimed only by
  `worker_max_tasks_per_child`. Marked with a `ponytail:` comment in the code.
- Making `asset_sourcer` failures loud instead of degrading to black frames.
- Credential key rotation, publishing, music mixing, Chatterbox TTS, Pixabay.

## Validation

- `.venv/bin/pytest` green after Shipment 1 and again after Shipment 2.
- Shipment 2 must change import paths only: no assertion, symbol name, or test count change.
- Baseline at time of writing: 109 tests across 9 files.
