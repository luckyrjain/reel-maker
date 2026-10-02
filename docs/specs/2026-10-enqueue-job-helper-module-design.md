# Shared router-side job enqueue — module design (Phase 7x)

Candidate 8 of the full-codebase `improve-codebase-architecture` review ("Strong"), settled via
`/grilling`.

## Problem

`api/routers/reels.py::create_reel`, `api/routers/cuts.py::trigger_render` and `trigger_publish`
each repeated the same sequence: add the Job, flush for its id, commit, `task.delay(job_id)`, on
failure `fail_unenqueued(...)` + a 503, then `db.refresh(job)`. Two of the copies
pointed at `trigger_render` in a comment instead of calling anything. The sequence is a correctness policy that lived
nowhere as a named interface:

- the owner-state change and the Job commit together;
- nothing stays open across the broker call (a slow broker failure would outlive an
  idle-in-transaction timeout and the `with_for_update` row lock);
- a broker failure fails the Job and frees its owner at once, otherwise the cut/reel sits in flight
  answering every retry with 409 until the reaper's 240-minute pending threshold;
- the id/type are read before the commit, because router sessions use `expire_on_commit=True`.

`fail_unenqueued` (worker/tasks/common.py) already owned the unwind half; the ordering around it
had no home.

## Decision

New `api/enqueue.py::enqueue_job(db, job, task, *, what)`:

- takes an **un-added** `Job` (the helper adds, flushes, captures `job.id` / `job.type.value`, then
  commits — so "capture before the commit" is not something a caller can forget) and the Celery
  **task object**, calling `task.delay(job_id)`;
- on failure calls `fail_unenqueued` and raises `HTTPException(503, "Could not queue the {what} —
  try again")` itself (precedent: `cut_media._resolve_within_video_store`), rather than returning a
  result each route would convert;
- refreshes the job on success (the commit expired it);
- `what` ("render" / "publish" / "job") only names the thing; the three original texts are kept.

Passing the task object, resolved from each route module's own globals, keeps every existing
`patch("api.routers.cuts.render_cut")` / `patch("api.routers.reels.enrich_context")` target valid, so
no existing test changed.

Lives in `api/enqueue.py`, not in the routers package (it is not a route) and not next to
`fail_unenqueued` (it raises an HTTP exception).

## Deliberately not shared

- `worker/tasks/enrich_context.py::_enqueue_generate` runs after its own done-stamp and hands a
  failure to `after_commit_failed` / `_abandon_generate`.
- `worker/tasks/maintenance.py` resume re-enqueues after commit and leaves a failed enqueue
  `pending` for its own pending-stale sweep.

Different failure policies and no HTTP; the helper's docstring says so.

## Test strategy

All 227 existing router / job-lifecycle tests ran unmodified against the extracted code first
(proof of no behavior change) and stay as wiring coverage. `tests/test_enqueue.py` (13) uses real
sessions with the default `expire_on_commit=True`: success commits, calls `delay(job.id)` and leaves
the job refreshed; the owner mutation and the Job land in one commit; commit precedes delay; no
transaction is open while delay runs; a broker failure raises a 503 whose text names the thing,
fails the Job and frees the owner for every realistic failure type (`ConnectionError`,
`TimeoutError`, `RuntimeError`, a kombu `OperationalError`); delay is never called twice.
12 mutations (delay before commit, no unwind, no refresh, id read after commit, wrong job type,
text ignoring `what`, no flush, swallowing the failure, a narrowed `except`, and a wrong `what` in
each of the three routes) each fail at least one test. 1026 default-run tests pass (+13).

One test-design catch worth recording: the first "job is refreshed" assertion read `job.id` before
inspecting expiry, which lazily reloads the row and hid a missing `db.refresh` — the no-refresh
mutation passed. The expiry state is now read before touching any attribute.

## Corrections

A 4-persona review on the opened PR (#39). Security and Correctness: clean (the new body is
statement-for-statement the old sequence; lock release, ordering, 503 texts, `job.type.value` vs the
old constants all verified identical). Real issues, all fixed:

1. **Test gap (Test-Quality, mutation-confirmed):** every broker-failure test raised
   `ConnectionError`, so narrowing `except Exception` to `except ConnectionError` left all 81 tests
   green. A real outage is a kombu `OperationalError`, a timeout or a bare `RuntimeError` — narrowing
   would reintroduce the exact bug the helper exists to prevent (a 500, the Job left pending, the
   owner stuck in flight). The failure test is now parametrized over four exception types.
2. **Test gap:** the 503 detail text was asserted only through `enqueue_job`, so a wrong `what` in any
   of the three routes survived. Each route-level 503 test now asserts the exact text.
3. **Docs:** the spec's test count (1019 was the pytest "passed" figure, excluding 3 skips; the
   default-run count is 1022 at PR open, 1026 after this round); "one copy" said "see
   trigger_render" — two did (`create_reel` and `trigger_publish`); a pre-existing wrong row in
   docs/architecture.md (`test_variants_router.py` 15, really 8).
4. **Process slip:** the first commit went up without its CLAUDE.md / architecture.md / api.md edits
   (a quoting bug in the edit script); a follow-up commit added them before any review started.

Not changed: a failure in `db.refresh(job)` after a successful `delay` would 500 on a job that does
run, and a raising `fail_unenqueued` replaces the 503 — both identical to main, not regressions.
