# Retry Semantics and Task-Module Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `max_retries` real for transient failures, turn deleted-row crashes into actionable job errors, backfill three untested paths, and lift pure content-generation helpers out of `worker/tasks/generate.py`.

**Architecture:** Two independent shipments. Shipment 1 (Tasks 1–9) changes behaviour only — no file moves. Shipment 2 (Tasks 10–12) moves files only — no behaviour change. Keeping them apart means a red suite after Shipment 2 can only be an import path.

**Tech Stack:** Python 3.11+, Celery 5.4 (bound tasks, `self.retry()`), SQLAlchemy 2.0, pytest 8 + `unittest.mock`, httpx.

**Spec:** `docs/superpowers/specs/2026-08-02-retry-and-task-cleanup-design.md`

## Global Constraints

- No new pip dependencies. `httpx` and `celery` are already direct dependencies.
- No Alembic migration — no schema changes anywhere in this plan.
- Run tests with `.venv/bin/pytest` (the project venv, never system Python).
- Baseline before Task 1: **109 tests across 9 files**, all passing.
- Shipment 2 moves symbols **verbatim, underscores intact**. Only `_heartbeat` is renamed (to `heartbeat`), because it genuinely becomes a cross-module API. No other rename.
- Never increment `job.attempts` in a retry branch — both tasks already increment it at task entry, and a retry re-enters at entry.
- `worker/tasks/enrich_context.py` keeps `max_retries=0`. Do not add a retry branch to it.
- Celery workers do not hot-reload. Not relevant during implementation (no live workers), but do not add "restart the worker" steps.
- Match existing style: no new docstrings beyond what the plan specifies, and only touch lines that trace to a task here.

---

# SHIPMENT 1 — failure handling

### Task 1: `worker/tasks/common.py` — transient-error classifier

**Files:**
- Create: `worker/tasks/common.py`
- Test: `tests/test_common.py` (new)

**Interfaces:**
- Consumes: nothing.
- Produces: `is_transient_error(exc: BaseException) -> bool` and `should_retry(exc: BaseException, retries: int, max_retries: int) -> bool`. Tasks 2 and 3 import `should_retry` from `worker.tasks.common`. Task 12 adds `heartbeat()` to this same module.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_common.py
"""Tests for worker/tasks/common.py — the retry-decision helpers."""
import subprocess

import httpx
import pytest

from worker.tasks.common import is_transient_error, should_retry


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/x")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.ConnectTimeout("timed out"), True),
        (httpx.ReadTimeout("timed out"), True),
        (httpx.ConnectError("refused"), True),
        (TimeoutError("timed out"), True),
        (subprocess.TimeoutExpired(cmd="ffprobe", timeout=5), True),
        (ConnectionError("reset"), True),
        (_status_error(429), True),
        (_status_error(503), True),
        (_status_error(500), True),
        (_status_error(404), False),
        (_status_error(422), False),
        (ValueError("quality score 40/100"), False),
        (RuntimeError("FFmpeg drawtext failed (exit 1)"), False),
    ],
)
def test_is_transient_error(exc, expected):
    assert is_transient_error(exc) is expected


def test_should_retry_true_for_transient_below_limit():
    assert should_retry(httpx.ConnectTimeout("x"), retries=0, max_retries=2) is True
    assert should_retry(httpx.ConnectTimeout("x"), retries=1, max_retries=2) is True


def test_should_retry_false_at_retry_limit():
    """A transient error still stops retrying once the budget is spent."""
    assert should_retry(httpx.ConnectTimeout("x"), retries=2, max_retries=2) is False


def test_should_retry_false_for_deterministic_error():
    assert should_retry(ValueError("bad guide"), retries=0, max_retries=2) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_common.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'worker.tasks.common'`

- [ ] **Step 3: Write minimal implementation**

```python
# worker/tasks/common.py
"""Shared helpers for Celery tasks.

is_transient_error / should_retry decide whether a failure is worth another
attempt. Kept as pure functions (no Celery, no db) so the decision is
unit-testable without a broker or a task context.
"""
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

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_common.py -v`
Expected: PASS — 16 passed.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/common.py tests/test_common.py
git commit -m "feat: add transient-error classifier for task retries"
```

---

### Task 2: `generate_guide` — null-guard the reel, wire retry

**Files:**
- Modify: `worker/tasks/generate.py:482` (add the guard), `worker/tasks/generate.py:637-641` (add the retry branch), and the import block at the top
- Test: `tests/test_generate_task.py` (new)

**Interfaces:**
- Consumes: `should_retry` from `worker.tasks.common` (Task 1).
- Produces: nothing later tasks depend on.

Context for the implementer: `generate_guide` currently reads `reel = db.get(models.Reel, job.reel_id)` and immediately does `reel.enriched_context or reel.context`. If the reel row was deleted, that raises `AttributeError: 'NoneType' object has no attribute 'enriched_context'`, which the outer handler stores verbatim in `job.error`. The outer handler at line 637 already sets `status = failed` and writes `str(exc)`, and its `transition()` call is guarded on `if reel and ...` — so raising a `ValueError` gets the right behaviour with no second failure path.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_generate_task.py
"""Tests for the generate_guide task's failure handling — missing rows and retries."""
from unittest.mock import MagicMock, patch

import httpx
import pytest
from celery.exceptions import Retry

from api import models


def _job(job_id=1, reel_id=10):
    job = MagicMock()
    job.id = job_id
    job.reel_id = reel_id
    job.cut_id = None
    job.status = models.JobStatus.pending
    job.attempts = 0
    job.progress = 0
    job.meta = {}
    job.error = None
    return job


def _reel():
    reel = MagicMock()
    reel.id = 10
    reel.context = "Argentina squad review."
    reel.enriched_context = None
    reel.niche = "football"
    reel.voiceover_mode = "voiceover"
    reel.status.value = "generating"
    return reel


def _cut():
    cut = MagicMock()
    cut.platform.value = "youtube_shorts"
    cut.target_length_s = 45.0
    return cut


def test_missing_reel_fails_job_with_actionable_message():
    """A deleted reel must produce an operator-readable error, not an AttributeError."""
    from worker.tasks.generate import generate_guide

    job = _job(reel_id=99)
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else None

    with patch("worker.tasks.generate.SessionLocal", return_value=db):
        with pytest.raises(ValueError, match="Reel 99 no longer exists"):
            generate_guide(1)

    assert job.status == models.JobStatus.failed
    assert "Reel 99 no longer exists" in job.error
    assert "AttributeError" not in job.error


def test_transient_failure_retries_and_resets_status_to_pending():
    """Retry must reset status to pending, or redelivery hits the idempotency guard."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.return_value.filter.return_value.all.return_value = [_cut()]

    with (
        patch("worker.tasks.generate.SessionLocal", return_value=db),
        patch("worker.tasks.generate.get_llm_provider",
              side_effect=httpx.ConnectTimeout("LLM unreachable")),
        patch.object(generate_guide, "retry", side_effect=Retry()) as mock_retry,
    ):
        with pytest.raises(Retry):
            generate_guide(1)

    mock_retry.assert_called_once()
    assert job.status == models.JobStatus.pending
    assert job.attempts == 1, "entry already incremented attempts; the retry branch must not"


def test_deterministic_failure_does_not_retry():
    """A bad-guide ValueError must fail once, exactly as before."""
    from worker.tasks.generate import generate_guide

    job = _job()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else reel
    db.query.return_value.filter.return_value.all.return_value = [_cut()]

    with (
        patch("worker.tasks.generate.SessionLocal", return_value=db),
        patch("worker.tasks.generate.get_llm_provider",
              side_effect=ValueError("model not found")),
        patch.object(generate_guide, "retry", side_effect=Retry()) as mock_retry,
    ):
        with pytest.raises(ValueError, match="model not found"):
            generate_guide(1)

    mock_retry.assert_not_called()
    assert job.status == models.JobStatus.failed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_generate_task.py -v`
Expected: all three FAIL. `test_missing_reel_...` fails with `AttributeError: 'NoneType' object has no attribute 'enriched_context'` instead of the expected `ValueError`; the retry test fails because `retry` is never called and status stays `failed`.

- [ ] **Step 3: Add the import**

In `worker/tasks/generate.py`, add to the import block (after the `from engine.observability import record_stage` line):

```python
from worker.celery_app import celery_app
from worker.tasks.common import should_retry
```

- [ ] **Step 4: Add the null guard**

In `worker/tasks/generate.py`, replace lines 482-483:

```python
        reel = db.get(models.Reel, job.reel_id)
        effective_context = reel.enriched_context or reel.context
```

with:

```python
        reel = db.get(models.Reel, job.reel_id)
        if reel is None:
            raise ValueError(f"Reel {job.reel_id} no longer exists")
        effective_context = reel.enriched_context or reel.context
```

- [ ] **Step 5: Add the retry branch**

In `worker/tasks/generate.py`, the outer handler currently starts:

```python
    except Exception as exc:
        db.rollback()
        job = db.get(models.Job, job_id)
        if job:
            job.status = models.JobStatus.failed
```

Insert the retry branch immediately after `db.rollback()`:

```python
    except Exception as exc:
        db.rollback()
        if should_retry(exc, self.request.retries, self.max_retries):
            job = db.get(models.Job, job_id)
            if job:
                # Reset to pending: the idempotency guard rejects `running`, so a
                # retry that left the status alone would be a silent no-op.
                job.status = models.JobStatus.pending
                job.error = f"transient failure, retry {self.request.retries + 1}: {exc}"[:2000]
                db.commit()
            raise self.retry(exc=exc, countdown=30 * 2 ** self.request.retries)
        job = db.get(models.Job, job_id)
        if job:
            job.status = models.JobStatus.failed
```

Leave the rest of the handler unchanged.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_generate_task.py -v`
Expected: PASS — 3 passed.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS — 128 passed (109 baseline + 16 from Task 1 + 3 here).

- [ ] **Step 8: Commit**

```bash
git add worker/tasks/generate.py tests/test_generate_task.py
git commit -m "fix: retry transient generate failures, guard missing reel"
```

---

### Task 3: `render_cut` — null-guard cut/reel, wire retry, clear stale error

**Files:**
- Modify: `worker/tasks/render.py:33-34` (guards), `worker/tasks/render.py:135` (clear `job.error`), `worker/tasks/render.py:138-140` (retry branch), and the import block
- Test: `tests/test_render_task.py` (new)

**Interfaces:**
- Consumes: `should_retry` from `worker.tasks.common` (Task 1).
- Produces: nothing later tasks depend on.

Context: `render_cut` does `cut = db.get(models.Cut, job.cut_id)` then immediately `reel = db.get(models.Reel, cut.reel_id)` — a deleted cut crashes on the very next line. Separately, the success path sets `job.status = done` but never clears `job.error`, and `ui/templates/fragments/render_status.html` renders `{% if job.error %}` regardless of status. Once the retry branch starts writing `job.error` during backoff, a retried-then-successful render would show the video with a red error under it. Both are fixed here.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_render_task.py
"""Tests for the render_cut task's failure handling and success-path cleanup."""
from unittest.mock import MagicMock, patch

import httpx
import pytest
from celery.exceptions import Retry

from api import models

_GUIDE = {
    "platform": "youtube_shorts",
    "target_length_s": 30.0,
    "caption": "Argentina's one weak spot could decide the tournament.",
    "hashtags": ["football"] * 10,
    "beats": [
        {"index": 0, "type": "hook", "duration_s": 3.0,
         "visual_direction": "Argentina squad walking out",
         "on_screen_text": ["One weak spot"], "vo_script": "One position could cost them everything."},
        {"index": 1, "type": "body", "duration_s": 10.0,
         "visual_direction": "Cristian Romero aggressive tackle",
         "on_screen_text": ["Romero presses"], "vo_script": "Romero's press lets them defend higher."},
        {"index": 2, "type": "cta", "duration_s": 5.0,
         "visual_direction": "Argentina squad celebrating",
         "on_screen_text": ["Your call"], "vo_script": "So what do you think? Drop it below."},
    ],
}


def _job(job_id=1, cut_id=5, reel_id=10):
    job = MagicMock()
    job.id = job_id
    job.cut_id = cut_id
    job.reel_id = reel_id
    job.status = models.JobStatus.pending
    job.attempts = 0
    job.progress = 0
    job.error = "stale error from a previous attempt"
    return job


def _cut():
    cut = MagicMock()
    cut.id = 5
    cut.reel_id = 10
    cut.guide = _GUIDE
    cut.platform.value = "youtube_shorts"
    cut.status.value = "rendering"
    return cut


def _reel():
    reel = MagicMock()
    reel.id = 10
    reel.voiceover_mode = "silent"
    return reel


def test_missing_cut_fails_job_with_actionable_message():
    """A deleted cut must not crash on cut.reel_id."""
    from worker.tasks.render import render_cut

    job = _job(cut_id=77)
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else None

    with patch("worker.tasks.render.SessionLocal", return_value=db):
        with pytest.raises(ValueError, match="Cut 77 no longer exists"):
            render_cut(1)

    assert job.status == models.JobStatus.failed
    assert "Cut 77 no longer exists" in job.error


def test_transient_failure_retries_and_resets_status_to_pending():
    from worker.tasks.render import render_cut

    job = _job()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else (
        _cut() if model is models.Cut else _reel()
    )

    with (
        patch("worker.tasks.render.SessionLocal", return_value=db),
        patch("worker.tasks.render.get_asset_sourcer",
              side_effect=httpx.ConnectTimeout("network down")),
        patch.object(render_cut, "retry", side_effect=Retry()) as mock_retry,
    ):
        with pytest.raises(Retry):
            render_cut(1)

    mock_retry.assert_called_once()
    assert job.status == models.JobStatus.pending
    assert job.attempts == 1


def test_successful_render_clears_stale_error():
    """A retried-then-successful render must not leave an error in the UI."""
    from worker.tasks.render import render_cut

    job = _job()
    cut = _cut()
    reel = _reel()
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else (
        cut if model is models.Cut else reel
    )

    with (
        patch("worker.tasks.render.SessionLocal", return_value=db),
        patch("worker.tasks.render.get_asset_sourcer"),
        patch("worker.tasks.render.get_wiki_sourcer"),
        patch("worker.tasks.render.get_hf_sourcer"),
        patch("worker.tasks.render.get_hf_video_sourcer"),
        patch("worker.tasks.render.get_tts_provider"),
        patch("worker.tasks.render.resolve_or_reuse", return_value=[(MagicMock(), None)]),
        patch("worker.tasks.render.record_stage"),
        patch("worker.tasks.render.composite_cut", return_value=18.0),
    ):
        render_cut(1)

    assert job.status == models.JobStatus.done
    assert job.error is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_render_task.py -v`
Expected: all three FAIL — the first with `AttributeError: 'NoneType' object has no attribute 'reel_id'`, the second because `retry` is never called, the third because `job.error` still holds the stale string.

- [ ] **Step 3: Add the import**

In `worker/tasks/render.py`, add below the existing `from worker.celery_app import celery_app` line:

```python
from worker.tasks.common import should_retry
```

- [ ] **Step 4: Add the null guards**

Replace lines 33-34:

```python
        cut = db.get(models.Cut, job.cut_id)
        reel = db.get(models.Reel, cut.reel_id)
```

with:

```python
        cut = db.get(models.Cut, job.cut_id)
        if cut is None:
            raise ValueError(f"Cut {job.cut_id} no longer exists")
        reel = db.get(models.Reel, cut.reel_id)
        if reel is None:
            raise ValueError(f"Reel {cut.reel_id} no longer exists")
```

- [ ] **Step 5: Clear `job.error` on the success path**

Replace:

```python
        job.progress = 100
        job.heartbeat_at = datetime.now(timezone.utc)
        job.status = models.JobStatus.done
        db.commit()
```

with:

```python
        job.progress = 100
        job.heartbeat_at = datetime.now(timezone.utc)
        job.status = models.JobStatus.done
        job.error = None   # clear any message left by a retried attempt
        db.commit()
```

- [ ] **Step 6: Add the retry branch**

Insert immediately after `db.rollback()` in the outer handler, exactly as in Task 2:

```python
    except Exception as exc:
        db.rollback()
        if should_retry(exc, self.request.retries, self.max_retries):
            job = db.get(models.Job, job_id)
            if job:
                # Reset to pending: the idempotency guard rejects `running`, so a
                # retry that left the status alone would be a silent no-op.
                job.status = models.JobStatus.pending
                job.error = f"transient failure, retry {self.request.retries + 1}: {exc}"[:2000]
                db.commit()
            raise self.retry(exc=exc, countdown=30 * 2 ** self.request.retries)
        job = db.get(models.Job, job_id)
        if job:
            job.status = models.JobStatus.failed
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_render_task.py -v`
Expected: PASS — 3 passed.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS — 131 passed.

- [ ] **Step 9: Commit**

```bash
git add worker/tasks/render.py tests/test_render_task.py
git commit -m "fix: retry transient render failures, guard missing cut, clear stale error"
```

---

### Task 4: `enrich_context` — null-guard the reel

**Files:**
- Modify: `worker/tasks/enrich_context.py:34`
- Test: `tests/test_enrich_context_task.py` (append)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: nothing.

No retry branch here — this task is deliberately `max_retries=0` (see Global Constraints).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_enrich_context_task.py`:

```python
# ── missing row guard ─────────────────────────────────────────────────────


def test_missing_reel_fails_job_with_actionable_message():
    """A deleted reel must produce an operator-readable error, not an AttributeError."""
    from api import models
    from worker.tasks.enrich_context import enrich_context

    job = _make_job()
    job.reel_id = 42
    db = MagicMock()
    db.get.side_effect = lambda model, _id: job if model is models.Job else None

    with patch("worker.tasks.enrich_context.SessionLocal", return_value=db):
        with pytest.raises(ValueError, match="Reel 42 no longer exists"):
            enrich_context(1)

    assert job.status == models.JobStatus.failed
    assert "Reel 42 no longer exists" in job.error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_enrich_context_task.py::test_missing_reel_fails_job_with_actionable_message -v`
Expected: FAIL — `AttributeError: 'NoneType' object has no attribute 'context'` raised from `evaluate_context(reel.context)`.

- [ ] **Step 3: Add the null guard**

In `worker/tasks/enrich_context.py`, replace line 34:

```python
        reel = db.get(models.Reel, job.reel_id)
```

with:

```python
        reel = db.get(models.Reel, job.reel_id)
        if reel is None:
            raise ValueError(f"Reel {job.reel_id} no longer exists")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_enrich_context_task.py -v`
Expected: PASS — 10 passed.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/enrich_context.py tests/test_enrich_context_task.py
git commit -m "fix: guard missing reel in enrich_context"
```

---

### Task 5: Reaper — key the pending branch on `updated_at`

**Files:**
- Modify: `worker/tasks/maintenance.py` (the `never_started` query)
- Test: `tests/test_maintenance.py` (append)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

Context: the pending branch currently filters `models.Job.created_at < pending_cutoff`. A long render created 40 minutes ago that fails transiently (Task 3) returns to `pending` and would be reaped mid-backoff on the next 60-second tick. The intended condition is "pending and untouched for 30 minutes", which is `updated_at`. `Job.updated_at` already has `onupdate=_now` (`api/models.py:148`), so the retry branch's commit refreshes it for free.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_maintenance.py`:

```python
def test_pending_branch_filters_on_updated_at_not_created_at():
    """A job that went back to pending for a retry must not be reaped mid-backoff.

    Filtering on created_at would reap an old long-running job the moment it
    entered retry backoff, because created_at never moves.
    """
    db = MagicMock()
    db.query.return_value.filter.return_value.all.side_effect = [[], []]

    with patch("worker.tasks.maintenance.SessionLocal", return_value=db):
        reap_stuck_jobs()

    filter_calls = db.query.return_value.filter.call_args_list
    assert len(filter_calls) == 2, "expected one running query and one pending query"
    pending_clause = str(filter_calls[1][0][1])
    assert "updated_at" in pending_clause
    assert "created_at" not in pending_clause
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_maintenance.py::test_pending_branch_filters_on_updated_at_not_created_at -v`
Expected: FAIL — `assert 'updated_at' in 'jobs.created_at < :created_at_1'`

- [ ] **Step 3: Change the filter column**

In `worker/tasks/maintenance.py`, in the `never_started` query, replace:

```python
                models.Job.created_at < pending_cutoff,
```

with:

```python
                models.Job.updated_at < pending_cutoff,
```

And update the module docstring line to match:

```python
  - `pending` with no update for PENDING_STALE_MINUTES — never picked up at all
    (broker down when .delay() was called, or no worker consuming the queue).
    Keyed on updated_at, not created_at, so a job sitting in retry backoff is
    not reaped for being old.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_maintenance.py -v`
Expected: PASS — 7 passed.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/maintenance.py tests/test_maintenance.py
git commit -m "fix: reap pending jobs by updated_at so retry backoff survives"
```

---

### Task 6: Test backfill — `resolve_or_reuse` re-pin path

**Files:**
- Test: `tests/test_asset_sourcer.py` (new)
- Modify: none — this task only adds coverage.

**Interfaces:**
- Consumes: `engine.render.asset_sourcer.resolve_or_reuse`, `SourcedAsset`, `_fp` (all pre-existing).
- Produces: nothing.

Context: `resolve_or_reuse` reuses pinned `CutAsset` rows when the `visual_direction` fingerprint matches, and deletes + re-pins when it changed. The delete-then-repin path has never been tested, and a bug there either duplicates rows (violating the `uq_cut_beat_order` constraint at render time) or silently keeps stale footage. This test uses a real in-memory SQLite session because the logic is a query/delete/insert sequence that a MagicMock cannot meaningfully exercise.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_asset_sourcer.py
"""Tests for resolve_or_reuse — the per-beat asset pinning ledger."""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api import models
from engine.render.asset_sourcer import SourcedAsset, _fp, resolve_or_reuse


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def cut(db):
    reel = models.Reel(context="Argentina squad review.", status=models.ReelStatus.draft)
    db.add(reel)
    db.flush()
    cut = models.Cut(
        reel_id=reel.id,
        platform=models.CutPlatform.youtube_shorts,
        target_length_s=45.0,
        status=models.CutStatus.draft,
    )
    db.add(cut)
    db.flush()
    return cut


class _StubSourcer:
    """Stands in for PexelsVideoSource — returns a distinct asset per query."""

    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.queries = []

    def search(self, query, min_duration_s):
        self.queries.append(query)
        ref = f"vid_{len(self.queries)}"
        return SourcedAsset(
            source="pexels",
            source_ref=ref,
            local_path=self.tmp_path / f"{ref}.mp4",
            license_str="pexels_free",
            safe_to_publish=True,
            duration_s=10.0,
        )


def _pins(db, cut):
    return (
        db.query(models.CutAsset)
        .filter(models.CutAsset.cut_id == cut.id, models.CutAsset.beat_index == 0)
        .all()
    )


def test_first_resolve_pins_the_asset(db, cut, tmp_path):
    sourcer = _StubSourcer(tmp_path)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero tackle",
                     min_duration_s=5.0, sourcer=sourcer)

    pins = _pins(db, cut)
    assert len(pins) == 1
    assert pins[0].resolved_from == _fp("Romero tackle")
    assert sourcer.queries == ["Romero tackle"]


def test_unchanged_direction_reuses_pin_without_calling_the_api(db, cut, tmp_path):
    sourcer = _StubSourcer(tmp_path)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero tackle",
                     min_duration_s=5.0, sourcer=sourcer)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero tackle",
                     min_duration_s=5.0, sourcer=sourcer)

    assert sourcer.queries == ["Romero tackle"], "second call must not hit the sourcer"
    assert len(_pins(db, cut)) == 1


def test_changed_direction_replaces_the_pin_rather_than_duplicating(db, cut, tmp_path):
    """The re-pin path must delete the stale row — a duplicate violates uq_cut_beat_order."""
    sourcer = _StubSourcer(tmp_path)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero tackle",
                     min_duration_s=5.0, sourcer=sourcer)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Messi through-ball",
                     min_duration_s=5.0, sourcer=sourcer)

    pins = _pins(db, cut)
    assert len(pins) == 1, "stale pin must be deleted, not left alongside the new one"
    assert pins[0].resolved_from == _fp("Messi through-ball")
    assert sourcer.queries == ["Romero tackle", "Messi through-ball"]


def test_other_beats_pins_are_untouched(db, cut, tmp_path):
    sourcer = _StubSourcer(tmp_path)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero tackle",
                     min_duration_s=5.0, sourcer=sourcer)
    resolve_or_reuse(db, cut=cut, beat_index=1, visual_direction="Messi through-ball",
                     min_duration_s=5.0, sourcer=sourcer)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="Romero header",
                     min_duration_s=5.0, sourcer=sourcer)

    beat_1 = (
        db.query(models.CutAsset)
        .filter(models.CutAsset.cut_id == cut.id, models.CutAsset.beat_index == 1)
        .all()
    )
    assert len(beat_1) == 1
    assert beat_1[0].resolved_from == _fp("Messi through-ball")
```

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/pytest tests/test_asset_sourcer.py -v`
Expected: PASS — 4 passed. These document existing correct behaviour; if any fail, that is a real bug in `resolve_or_reuse` — stop and report it rather than editing the test to match.

- [ ] **Step 3: Commit**

```bash
git add tests/test_asset_sourcer.py
git commit -m "test: cover resolve_or_reuse pin, reuse, and re-pin paths"
```

---

### Task 7: Test backfill — `judge_guide` failure fallback

**Files:**
- Test: `tests/test_llm_judge.py` (new)
- Modify: none.

**Interfaces:**
- Consumes: `engine.generation.llm_judge.judge_guide` (pre-existing).
- Produces: nothing.

Context: `judge_guide` must never propagate an LLM failure — generation would abort on a judging problem rather than a quality problem. It returns a neutral `(50, [warning])` instead. That contract is load-bearing for `_combined_score` and has no test.

- [ ] **Step 1: Write the test**

```python
# tests/test_llm_judge.py
"""Tests for judge_guide's failure fallback — judging must never block generation."""
import httpx

from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
from engine.generation.llm_judge import judge_guide


def _guide() -> MasterGuide:
    beats = [
        Beat(index=0, type="hook", duration_s=3.0, visual_direction="Argentina squad",
             on_screen_text=["One weak spot"], vo_script="One position could cost them everything."),
        Beat(index=1, type="body", duration_s=10.0, visual_direction="Cristian Romero tackle",
             on_screen_text=["Romero presses"], vo_script="Romero's press lets them defend higher."),
        Beat(index=2, type="cta", duration_s=5.0, visual_direction="Argentina celebrating",
             on_screen_text=["Your call"], vo_script="So what do you think? Drop it below."),
    ]
    return MasterGuide(
        title="Test", niche="football",
        cuts=[PlatformGuide(platform="youtube_shorts", target_length_s=30.0,
                            caption="Argentina's one weak spot.", hashtags=["football"] * 10,
                            beats=beats)],
    )


class _RaisingProvider:
    def complete(self, messages, json_mode=True):
        raise httpx.ConnectTimeout("judge unreachable")


class _GarbageProvider:
    def complete(self, messages, json_mode=True):
        return "I think this script is pretty good, honestly."


class _OutOfRangeProvider:
    def complete(self, messages, json_mode=True):
        return '{"factual_accuracy": 99, "expertise_depth": 5, "natural_speech": 5, ' \
               '"hallucination_risk": 5, "shareability": 5}'


def test_provider_exception_returns_neutral_score():
    score, issues = judge_guide("Argentina squad review.", _guide(), _RaisingProvider())
    assert score == 50
    assert issues and "judge unavailable" in issues[0].lower()


def test_unparseable_response_returns_neutral_score():
    score, issues = judge_guide("Argentina squad review.", _guide(), _GarbageProvider())
    assert score == 50
    assert issues


def test_out_of_range_dimension_returns_neutral_score():
    """Pydantic bounds (0-20) reject the payload; that must fall back, not raise."""
    score, issues = judge_guide("Argentina squad review.", _guide(), _OutOfRangeProvider())
    assert score == 50
    assert issues
```

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/pytest tests/test_llm_judge.py -v`
Expected: PASS — 3 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_llm_judge.py
git commit -m "test: cover judge_guide neutral-score fallback"
```

---

### Task 8: Test backfill — `synth_to_budget` rate clamp

**Files:**
- Test: `tests/test_tts.py` (append)
- Modify: none.

**Interfaces:**
- Consumes: `engine.render.tts.EdgeTTSProvider` (pre-existing).
- Produces: nothing.

Context: `synth_to_budget` re-synthesizes at an adjusted speaking rate when measured duration drifts more than 15% from the beat's target, clamping the rate to ±25% so the voice stays natural. Sign and clamp are easy to get backwards and untested today. Positive rate = faster; `drift > 0` means the audio ran long, so it must speed up.

- [ ] **Step 1: Write the test**

Append to `tests/test_tts.py`:

```python
# ── synth_to_budget rate clamping ─────────────────────────────────────────


def _budget_provider(tmp_path, measured_s):
    """EdgeTTSProvider with synthesize stubbed to record rate and a fixed measured duration."""
    provider = EdgeTTSProvider(tmp_path)
    calls = []

    def fake_synthesize(text, rate="+0%"):
        calls.append(rate)
        return tmp_path / "out.mp3"

    provider.synthesize = fake_synthesize
    return provider, calls


def test_no_resynth_when_within_tolerance(tmp_path):
    provider, calls = _budget_provider(tmp_path, 10.5)
    with patch("engine.render.tts._audio_duration", return_value=10.5):
        provider.synth_to_budget("some narration", target_s=10.0)
    assert calls == ["+0%"], "5% drift is inside the 15% tolerance — no second synth"


def test_overlong_audio_speeds_up_and_clamps_to_plus_25(tmp_path):
    """14s of audio for a 10s beat is +40% drift; the rate must clamp to +25%."""
    provider, calls = _budget_provider(tmp_path, 14.0)
    with patch("engine.render.tts._audio_duration", return_value=14.0):
        provider.synth_to_budget("some narration", target_s=10.0)
    assert calls == ["+0%", "+25%"]


def test_short_audio_slows_down_and_clamps_to_minus_25(tmp_path):
    """5s of audio for a 10s beat is -50% drift; the rate must clamp to -25%."""
    provider, calls = _budget_provider(tmp_path, 5.0)
    with patch("engine.render.tts._audio_duration", return_value=5.0):
        provider.synth_to_budget("some narration", target_s=10.0)
    assert calls == ["+0%", "-25%"]


def test_unmeasurable_audio_returns_first_take(tmp_path):
    provider, calls = _budget_provider(tmp_path, None)
    with patch("engine.render.tts._audio_duration", return_value=None):
        provider.synth_to_budget("some narration", target_s=10.0)
    assert calls == ["+0%"]
```

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/pytest tests/test_tts.py -v`
Expected: PASS — 8 passed.

- [ ] **Step 3: Commit**

```bash
git add tests/test_tts.py
git commit -m "test: cover synth_to_budget rate sign and clamp boundaries"
```

---

### Task 9: Document Shipment 1 in `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md` — Architecture section, Reliability features list, Module layout tree, test counts
- Test: none (docs only).

**Interfaces:**
- Consumes: nothing.
- Produces: nothing.

- [ ] **Step 1: Add the `enrich_context` stage to the Architecture section**

`CLAUDE.md`'s **Job pipeline** paragraph currently describes `POST /api/reels` as enqueuing guide generation directly. Immediately after the `**Job pipeline:**` paragraph, insert:

```markdown
**Context enrichment stage:** `POST /api/reels` does not enqueue generation directly. It creates the
`Reel` + `Cut` rows and an `enrich` job, and enqueues `enrich_context`. That task scores the raw
context with `evaluate_context()` (5 axes × 20 pts), calls `llm_enrich()` when the score is below 60
— skipped entirely for structured scripts, where enrichment would cause topic drift — stores the
result on `reel.enriched_context`, transitions the reel to `generating`, then creates and enqueues
the `generate_guide` job. `generate_guide` reads `reel.enriched_context or reel.context`.
```

- [ ] **Step 2: Document the retry behaviour**

In the **Reliability features** list, after the `task_reject_on_worker_lost=True` bullet, add:

```markdown
- **Transient-failure retry** — `generate_guide` and `render_cut` retry up to twice with 30 s/60 s
  backoff when `worker/tasks/common.py::should_retry()` classifies the exception as transient
  (httpx transport errors, timeouts, HTTP 429/5xx). Deterministic failures — bad LLM JSON,
  quality-below-threshold, a missing row, ffmpeg's non-zero exit — fail once, unchanged. The retry
  branch resets `job.status` to `pending` before calling `self.retry()`: the idempotency guard
  rejects `running`, so a retry that left the status alone would be a silent no-op. It must **not**
  bump `job.attempts` — task entry already does. `enrich_context` stays `max_retries=0` on purpose.
```

- [ ] **Step 3: Update the heartbeat bullet**

Replace the existing pending-reap wording so it matches Task 5:

```markdown
- **Heartbeat** — tasks write `job.heartbeat_at` at every milestone. `reap_stuck_jobs` (Celery beat,
  every 60 s) fails any `running` job without a heartbeat update in the last 5 minutes, **and** any
  `pending` job whose `updated_at` is older than 30 minutes (broker was down when `.delay()` ran, or
  no worker consumes the queue). The pending branch keys on `updated_at`, not `created_at`, so a job
  sitting in retry backoff is not reaped for being old. Both roll back the owning reel
  (`enriching` or `generating`) / cut.
```

- [ ] **Step 4: Add `common.py` to the module layout**

In the `worker/` block of the Module layout tree, add above the `tasks/` entries:

```
  tasks/
    common.py         should_retry() / is_transient_error() — retry classification
```

- [ ] **Step 5: Update the test counts**

- In the Commands block: `# 109 tests across 9 files` → `# 144 tests across 14 files`
- In the tests listing, add:

```
  test_common.py              16 tests — transient-error classification, retry budget
  test_generate_task.py        3 tests — missing reel, transient retry, deterministic failure
  test_render_task.py          3 tests — missing cut, transient retry, success clears stale error
  test_asset_sourcer.py        4 tests — resolve_or_reuse pin, reuse, re-pin, beat isolation
  test_llm_judge.py            3 tests — neutral-score fallback on raise, garbage, out-of-range
```

- and update the amended lines: `test_enrich_context_task.py` 9 → 10 tests, `test_maintenance.py` 6 → 7 tests, `test_tts.py` 4 → 8 tests.

- [ ] **Step 6: Verify the counts are true**

Run: `.venv/bin/pytest -q`
Expected: PASS — 144 passed (109 baseline + 16 + 3 + 3 + 1 + 1 + 4 + 3 + 4). If the number differs, correct `CLAUDE.md` to the real number rather than the other way round.

- [ ] **Step 7: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document enrich_context stage and transient-failure retry"
```

---

# SHIPMENT 2 — module split

Behaviour must not change. Every symbol moves verbatim with its leading underscore. If any test assertion or symbol name needs editing, stop — that means something was not a pure move.

### Task 10: Extract `engine/generation/visual_fallback.py`

**Files:**
- Create: `engine/generation/visual_fallback.py`
- Modify: `worker/tasks/generate.py:30-124` (delete the moved block), import block
- Test: none new — the full suite is the regression test.

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_SECTION_FALLBACK_VISUALS`, `_VO_TO_SHOT`, `_fallback_visual(stub: BeatStub) -> str`, `_VO_NAME_RE`, `_NON_PERSON`, `_NON_PERSON_PREFIXES`, `_first_person(vo: str, existing: str) -> str`, `_DEGENERATE_SUFFIXES`, `_DEGENERATE_PHRASES`, `_is_degenerate_visual(v: str) -> bool` — all importable from `engine.generation.visual_fallback`. Task 11 does not depend on these; `generate.py` imports `_fallback_visual`, `_first_person`, and `_is_degenerate_visual`.

- [ ] **Step 1: Create the new module**

Create `engine/generation/visual_fallback.py` with this header, then move — cut, do not copy — lines 30 through 124 of `worker/tasks/generate.py` into it verbatim (from `_SECTION_FALLBACK_VISUALS: dict[str, str] = {` through the end of `_is_degenerate_visual`):

```python
"""Fallback visual_direction synthesis for beats the visuals LLM did not cover.

Pure content generation — no Celery, no db, no Job. Imported by
worker/tasks/generate.py when the LLM returns a degenerate or missing
visual_direction for a beat.
"""
import re as _re

from engine.generation.script_parser import BeatStub

# ... moved block goes here, unchanged ...
```

- [ ] **Step 2: Wire the imports in `generate.py`**

In `worker/tasks/generate.py`, add to the import block:

```python
from engine.generation.visual_fallback import (
    _fallback_visual,
    _first_person,
    _is_degenerate_visual,
)
```

Then delete the now-moved block from `generate.py`. Check whether `import re as _re` is still needed there — `_TACTICAL_MARKERS` and `_CONFLICT_RE` still use it until Task 11, so keep it for now.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS — 144 passed, same as at the end of Task 9. Any failure here is an import error, not a behaviour change.

- [ ] **Step 4: Verify nothing else referenced the moved names**

Run: `grep -rn "_fallback_visual\|_first_person\|_is_degenerate_visual\|_VO_TO_SHOT\|_SECTION_FALLBACK_VISUALS" api engine worker tests | grep -v __pycache__`
Expected: references only from `engine/generation/visual_fallback.py` and the import line plus call sites in `worker/tasks/generate.py`.

- [ ] **Step 5: Commit**

```bash
git add engine/generation/visual_fallback.py worker/tasks/generate.py
git commit -m "refactor: move visual fallback helpers to engine/generation"
```

---

### Task 11: Extract `engine/generation/beat_enrichment.py`

**Files:**
- Create: `engine/generation/beat_enrichment.py`
- Modify: `worker/tasks/generate.py` (delete the moved block, add imports), `tests/test_enrichment.py:126,148` (import paths)
- Test: `tests/test_enrichment.py` — existing assertions unchanged, imports repointed.

**Interfaces:**
- Consumes: `_is_shallow_beat` is used by `_generate_from_structured_script`, which stays in `generate.py`.
- Produces: `_TACTICAL_MARKERS`, `_is_shallow_beat(stub: BeatStub) -> bool`, `_ENRICH_BATCH`, `_enrich_batch(batch: list[BeatStub], context: str, llm) -> dict[int, str]`, `_enrich_with_insight(stubs: list[BeatStub], context: str, llm) -> None`, `_CONFLICT_RE`, `_has_conflict_beat(stubs: list[BeatStub]) -> bool`, `_make_conflict_stub(context: str, llm, index: int) -> tuple[BeatStub, str] | None` — all from `engine.generation.beat_enrichment`.

- [ ] **Step 1: Create the new module**

Create `engine/generation/beat_enrichment.py` and move — cut, not copy — the `_TACTICAL_MARKERS` / `_is_shallow_beat` / `_ENRICH_BATCH` / `_enrich_batch` / `_enrich_with_insight` / `_CONFLICT_RE` / `_has_conflict_beat` / `_make_conflict_stub` block out of `worker/tasks/generate.py` verbatim, under this header:

```python
"""Beat-level insight enrichment and conflict-beat synthesis.

Pure content generation — no Celery, no db, no Job. Both prompts carry an
explicit topic fence ("Do not introduce matches, tournaments, scorelines, or
players not mentioned...") to stop the LLM drifting into unrelated events.
"""
import json
import re as _re

from engine.generation.script_parser import BeatStub, calc_duration, derive_on_screen

# ... moved block goes here, unchanged ...
```

- [ ] **Step 2: Wire the imports in `generate.py`**

Add to `worker/tasks/generate.py`:

```python
from engine.generation.beat_enrichment import (
    _enrich_with_insight,
    _has_conflict_beat,
    _is_shallow_beat,
    _make_conflict_stub,
)
```

`generate.py` no longer uses `_enrich_batch` directly — do not import it. Now remove any imports in `generate.py` that have become unused: `json` and `re as _re` are both candidates. Check with `grep -n "json\.\|_re\." worker/tasks/generate.py` and drop whichever no longer appears. `calc_duration` and `derive_on_screen` from `script_parser` also move with `_make_conflict_stub` — check whether `generate.py` still uses them and trim its `script_parser` import accordingly.

- [ ] **Step 3: Repoint the test imports**

In `tests/test_enrichment.py`, line 126 and line 148:

```python
from worker.tasks.generate import _enrich_batch      # line 126
from worker.tasks.generate import _make_conflict_stub  # line 148
```

become:

```python
from engine.generation.beat_enrichment import _enrich_batch      # line 126
from engine.generation.beat_enrichment import _make_conflict_stub  # line 148
```

Change nothing else in that file — no assertion, no symbol name.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS — 144 passed.

- [ ] **Step 5: Check the line count moved as intended**

Run: `wc -l worker/tasks/generate.py`
Expected: roughly 350 lines, down from 652.

- [ ] **Step 6: Commit**

```bash
git add engine/generation/beat_enrichment.py worker/tasks/generate.py tests/test_enrichment.py
git commit -m "refactor: move beat enrichment helpers to engine/generation"
```

---

### Task 12: Share `heartbeat()`, finish the docs

**Files:**
- Modify: `worker/tasks/common.py` (add `heartbeat`), `worker/tasks/generate.py`, `worker/tasks/render.py`, `worker/tasks/enrich_context.py` (delete the three copies), `CLAUDE.md` (module layout)
- Test: none new — the full suite covers it.

**Interfaces:**
- Consumes: `worker/tasks/common.py` from Task 1.
- Produces: `heartbeat(db, job, progress: int) -> None`. This is the one deliberate rename in Shipment 2 — it becomes a cross-module API, and three diverging copies is exactly the failure being fixed.

- [ ] **Step 1: Add `heartbeat` to `common.py`**

Append to `worker/tasks/common.py`:

```python
from datetime import datetime, timezone


def heartbeat(db, job, progress: int) -> None:
    """Record progress and prove the worker is alive.

    reap_stuck_jobs fails any running job whose heartbeat_at goes stale for
    more than STALE_MINUTES, so every long task must call this at each milestone.
    """
    job.progress = progress
    job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()
```

Move the `from datetime import ...` line up into the module's import block rather than leaving it mid-file.

- [ ] **Step 2: Replace the three copies**

In each of `worker/tasks/generate.py`, `worker/tasks/render.py`, and `worker/tasks/enrich_context.py`: delete the local `def _heartbeat(db, job, progress: int) -> None:` definition, and change the existing import of `should_retry` (or add one, in `enrich_context.py`, which has no retry branch) to:

```python
from worker.tasks.common import heartbeat
```

`generate.py` and `render.py` import both: `from worker.tasks.common import heartbeat, should_retry`.

Then rename every call site from `_heartbeat(` to `heartbeat(`. Find them with:

Run: `grep -rn "_heartbeat(" worker/ | grep -v __pycache__`

- [ ] **Step 3: Verify no copies remain**

Run: `grep -rn "def _heartbeat" worker/ | grep -v __pycache__`
Expected: no output.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS — 144 passed.

- [ ] **Step 5: Update the module layout in `CLAUDE.md`**

In the `engine/generation/` block of the Module layout tree, add:

```
    visual_fallback.py  Fallback visual_direction synthesis — section/VO keyword tables
    beat_enrichment.py  Tactical insight enrichment + conflict-beat synthesis (topic-fenced)
```

And extend the `worker/tasks/common.py` line added in Task 9:

```
    common.py         should_retry() / is_transient_error() / heartbeat() — shared task helpers
```

Add a conventions bullet near the existing "Every Celery task" bullet:

```markdown
- **`heartbeat()` lives in `worker/tasks/common.py`** — never redefine it per task file. Three
  copies previously drifted apart; the reaper depends on all tasks writing `heartbeat_at` the
  same way.
```

- [ ] **Step 6: Commit**

```bash
git add worker/tasks/common.py worker/tasks/generate.py worker/tasks/render.py worker/tasks/enrich_context.py CLAUDE.md
git commit -m "refactor: share heartbeat() across tasks"
```

---

## Done criteria

- `.venv/bin/pytest` — 144 passed.
- `grep -rn "def _heartbeat" worker/` — no output.
- `wc -l worker/tasks/generate.py` — ~350.
- `grep -rn "self.retry" worker/` — two hits (`generate.py`, `render.py`), none in `enrich_context.py`.
- Shipment 2's three commits changed no test assertion and no symbol name except `_heartbeat` → `heartbeat`.

## Known limitation, carried from the spec

Every test here is a unit test against a mocked session or in-memory SQLite. Nothing in this plan is
verified against Postgres, Redis, a live worker, or ffmpeg. A real end-to-end run — submit a reel,
watch it enrich → generate → render — is still worth doing before trusting this in production,
together with the render-path fixes from `b7e444c`.
