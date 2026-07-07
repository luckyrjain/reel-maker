# Reliability, Arch Cleanup, and Doc-Drift Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix a stuck-reel reaper gap, wire real Celery retry for transient failures, null-guard deleted-row lookups, split an oversized task file into focused modules, correct stale docs, and backfill four untested-but-fragile code paths — per `docs/superpowers/specs/2026-07-06-reliability-and-cleanup-fixes.md`.

**Architecture:** Ten independently-committable surgical changes across `worker/tasks/`, `engine/generation/`, and `CLAUDE.md`. No new dependencies, no DB schema changes, no behavior change to deterministic failure paths (only transient/network failures get new retry behavior).

**Tech Stack:** Python, Celery (bound tasks, `self.retry()`), pytest + `unittest.mock`, SQLAlchemy (one test uses a real in-memory SQLite session), httpx (exception classification).

## Global Constraints

- No new pip dependencies.
- No Alembic migration — no schema changes in this plan.
- Run tests with `.venv/bin/pytest` (project's venv, not system Python).
- Celery workers do not hot-reload — not relevant to plan execution (no live workers run during implementation), but call out in Task 10's doc update.
- Match existing code style: no docstrings added beyond what's already there; only touch lines that trace to a task in this plan.
- Every new/moved function that is `_`-prefixed in its **new** home module per the spec becomes public (no leading underscore) — see spec Fix 4 for the exact rename table. Do not rename anything not listed there.

---

### Task 1: Reaper — revert `enriching` reels, not just `generating`

**Files:**
- Modify: `worker/tasks/maintenance.py:43-58`
- Test: `tests/test_maintenance.py` (new)

**Interfaces:**
- Consumes: `api.state.REEL_TRANSITIONS`, `api.state.transition`, `api.models.Job`, `api.models.Reel`, `api.models.Cut` (all pre-existing, unchanged).
- Produces: nothing new consumed by later tasks — this is a leaf fix.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_maintenance.py
"""Tests for reap_stuck_jobs / _revert_owner — reel/cut revert-on-stall logic."""
from unittest.mock import MagicMock, patch

from worker.tasks.maintenance import _revert_owner, REEL_TRANSITIONS, reap_stuck_jobs


def _make_job(reel_id=None, cut_id=None):
    job = MagicMock()
    job.reel_id = reel_id
    job.cut_id = cut_id
    return job


def test_revert_owner_fails_enriching_reel():
    """Reaper must revert a reel stuck in 'enriching', not only 'generating'."""
    job = _make_job(reel_id=5)
    reel = MagicMock()
    reel.status.value = "enriching"
    db = MagicMock()
    db.get.return_value = reel

    with patch("worker.tasks.maintenance.transition") as mock_transition:
        _revert_owner(db, job)

    mock_transition.assert_called_once_with(reel, "failed", REEL_TRANSITIONS)


def test_revert_owner_fails_generating_reel():
    """Regression: existing 'generating' revert path must keep working."""
    job = _make_job(reel_id=5)
    reel = MagicMock()
    reel.status.value = "generating"
    db = MagicMock()
    db.get.return_value = reel

    with patch("worker.tasks.maintenance.transition") as mock_transition:
        _revert_owner(db, job)

    mock_transition.assert_called_once_with(reel, "failed", REEL_TRANSITIONS)


def test_revert_owner_ignores_reel_in_other_status():
    """A reel already past guide_ready must not be force-transitioned."""
    job = _make_job(reel_id=5)
    reel = MagicMock()
    reel.status.value = "guide_ready"
    db = MagicMock()
    db.get.return_value = reel

    with patch("worker.tasks.maintenance.transition") as mock_transition:
        _revert_owner(db, job)

    mock_transition.assert_not_called()


def test_reap_stuck_jobs_reverts_enriching_reel_end_to_end():
    """Full reap_stuck_jobs() pass: a stale 'enriching' reel is failed, not left stuck."""
    job = MagicMock()
    job.reel_id = 7
    job.cut_id = None

    reel = MagicMock()
    reel.status.value = "enriching"

    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [job]
    db.get.return_value = reel

    with (
        patch("worker.tasks.maintenance.SessionLocal", return_value=db),
        patch("worker.tasks.maintenance.transition") as mock_transition,
    ):
        reap_stuck_jobs()

    assert job.status == __import__("api.models", fromlist=["JobStatus"]).JobStatus.failed
    mock_transition.assert_called_once_with(reel, "failed", REEL_TRANSITIONS)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_maintenance.py -v`
Expected: `test_revert_owner_fails_enriching_reel` FAILS (`mock_transition` not called — current code only checks `"generating"`). The other three should already pass (regression coverage).

- [ ] **Step 3: Fix `_revert_owner`**

In `worker/tasks/maintenance.py`, replace:

```python
def _revert_owner(db, job: models.Job) -> None:
    """Roll the reel/cut back to a state where the operator can retry."""
    if job.reel_id:
        reel = db.get(models.Reel, job.reel_id)
        if reel and reel.status.value == "generating":
            try:
                transition(reel, "failed", REEL_TRANSITIONS)
            except ValueError:
                pass
    if job.cut_id:
        cut = db.get(models.Cut, job.cut_id)
        if cut and cut.status.value == "rendering":
            try:
                transition(cut, "failed", CUT_TRANSITIONS)
            except ValueError:
                pass
```

with:

```python
def _revert_owner(db, job: models.Job) -> None:
    """Roll the reel/cut back to a state where the operator can retry."""
    if job.reel_id:
        reel = db.get(models.Reel, job.reel_id)
        if reel and reel.status.value in ("generating", "enriching"):
            try:
                transition(reel, "failed", REEL_TRANSITIONS)
            except ValueError:
                pass
    if job.cut_id:
        cut = db.get(models.Cut, job.cut_id)
        if cut and cut.status.value == "rendering":
            try:
                transition(cut, "failed", CUT_TRANSITIONS)
            except ValueError:
                pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_maintenance.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/maintenance.py tests/test_maintenance.py
git commit -m "$(cat <<'EOF'
fix: reaper now reverts reels stuck in enriching, not just generating

A worker dying mid-enrich_context left the reel permanently in
"enriching" with no revert path — reap_stuck_jobs only checked
"generating". REEL_TRANSITIONS already permits enriching->failed.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Shared `worker/tasks/common.py` — `heartbeat()` + `is_transient_error()`

**Files:**
- Create: `worker/tasks/common.py`
- Test: `tests/test_worker_common.py` (new)

**Interfaces:**
- Produces: `heartbeat(db, job, progress: int) -> None`, `is_transient_error(exc: Exception) -> bool` — both consumed by Tasks 3, 4, 5.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_worker_common.py
"""Tests for worker/tasks/common.py — shared heartbeat + transient-error classification."""
import subprocess
from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from worker.tasks.common import heartbeat, is_transient_error


def test_heartbeat_updates_progress_and_commits():
    db = MagicMock()
    job = MagicMock()
    heartbeat(db, job, 42)
    assert job.progress == 42
    assert isinstance(job.heartbeat_at, datetime)
    assert job.heartbeat_at.tzinfo is timezone.utc
    db.commit.assert_called_once()


@pytest.mark.parametrize("exc", [
    httpx.ConnectTimeout("timeout"),
    httpx.ConnectError("refused"),
    httpx.ReadTimeout("read timeout"),
    ConnectionError("reset"),
    subprocess.TimeoutExpired(cmd="ffmpeg", timeout=5),
])
def test_transient_errors_return_true(exc):
    assert is_transient_error(exc) is True


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504])
def test_transient_http_status_codes_return_true(status_code):
    request = httpx.Request("GET", "http://example.com")
    response = httpx.Response(status_code, request=request)
    exc = httpx.HTTPStatusError("error", request=request, response=response)
    assert is_transient_error(exc) is True


def test_non_transient_http_status_404_returns_false():
    request = httpx.Request("GET", "http://example.com")
    response = httpx.Response(404, request=request)
    exc = httpx.HTTPStatusError("error", request=request, response=response)
    assert is_transient_error(exc) is False


@pytest.mark.parametrize("exc", [
    ValueError("bad guide quality"),
    KeyError("missing"),
    RuntimeError("logic bug"),
])
def test_non_transient_errors_return_false(exc):
    assert is_transient_error(exc) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_worker_common.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'worker.tasks.common'`.

- [ ] **Step 3: Create `worker/tasks/common.py`**

```python
from datetime import datetime, timezone
import subprocess

import httpx

_TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


def heartbeat(db, job, progress: int) -> None:
    job.progress = progress
    job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()


def is_transient_error(exc: Exception) -> bool:
    """True for network/IO failures worth retrying; False for deterministic failures
    (bad LLM JSON, quality-below-threshold, validation errors) where retrying with
    the same input produces the same result."""
    if isinstance(exc, (httpx.TransportError, TimeoutError, subprocess.TimeoutExpired, ConnectionError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS_CODES
    return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_worker_common.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add worker/tasks/common.py tests/test_worker_common.py
git commit -m "$(cat <<'EOF'
feat: add shared heartbeat() and is_transient_error() for worker tasks

Prep for wiring real Celery retry in generate_guide/render_cut (Tasks
3-4) and deduping the three copies of _heartbeat (Tasks 3-5).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: `generate_guide` — null-guard missing reel + wire real retry

**Files:**
- Modify: `worker/tasks/generate.py:1-27` (imports), `:465-482` (drop local `_heartbeat`, add null-guard), `:637-650` (except block)
- Test: `tests/test_generate_task.py` (new)

**Interfaces:**
- Consumes: `worker.tasks.common.heartbeat` (aliased as `_heartbeat`), `worker.tasks.common.is_transient_error` (from Task 2).
- Produces: no new public interface — task-level behavior only.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_generate_task.py
"""Task-level tests for generate_guide: missing-reel guard, transient-retry dispatch."""
from unittest.mock import MagicMock, patch

import httpx
import pytest
from celery.exceptions import Retry

from api import models


def _make_job(job_id=1, reel_id=10, meta=None):
    job = MagicMock()
    job.id = job_id
    job.reel_id = reel_id
    job.status = "pending"
    job.meta = meta or {"generation_path": "auto"}
    job.attempts = 0
    return job


def test_missing_reel_fails_job_cleanly():
    from worker.tasks.generate import generate_guide

    job = _make_job(reel_id=10)
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if model is models.Job else None

    with patch("worker.tasks.generate.SessionLocal", return_value=db):
        generate_guide(1)

    assert job.status == models.JobStatus.failed
    assert "10" in job.error


def test_transient_error_triggers_retry_with_backoff():
    from worker.tasks.generate import generate_guide

    job = _make_job(reel_id=10)
    reel = MagicMock()
    reel.id = 10
    reel.enriched_context = None
    reel.context = "some context"

    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if model is models.Job else reel
    db.query.side_effect = httpx.ConnectError("connection refused")

    with (
        patch("worker.tasks.generate.SessionLocal", return_value=db),
        patch.object(generate_guide, "retry", side_effect=Retry("retry")) as mock_retry,
    ):
        with pytest.raises(Retry):
            generate_guide(1)

    mock_retry.assert_called_once()
    _, kwargs = mock_retry.call_args
    assert isinstance(kwargs["exc"], httpx.ConnectError)
    assert kwargs["countdown"] == 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_generate_task.py -v`
Expected: `test_missing_reel_fails_job_cleanly` FAILS (`AttributeError` on `reel.enriched_context` since `reel` is `None` today). `test_transient_error_triggers_retry_with_backoff` FAILS (no retry ever called — task just fails).

- [ ] **Step 3: Implement — imports, null-guard, drop local `_heartbeat`**

In `worker/tasks/generate.py`, change the import block (currently ends around line 21):

```python
from engine.observability import record_stage
from worker.celery_app import celery_app
```

to:

```python
from engine.observability import record_stage
from worker.celery_app import celery_app
from worker.tasks.common import heartbeat as _heartbeat, is_transient_error
```

Delete the local `_heartbeat` definition (currently directly above `generate_guide`):

```python
def _heartbeat(db, job, progress: int) -> None:
    job.progress = progress
    job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()


@celery_app.task(bind=True, max_retries=2)
def generate_guide(self, job_id: int):
```

becomes:

```python
@celery_app.task(bind=True, max_retries=2)
def generate_guide(self, job_id: int):
```

Add the null-guard right after the reel fetch:

```python
        reel = db.get(models.Reel, job.reel_id)
        effective_context = reel.enriched_context or reel.context
```

becomes:

```python
        reel = db.get(models.Reel, job.reel_id)
        if reel is None:
            job.status = models.JobStatus.failed
            job.error = f"Reel {job.reel_id} no longer exists"
            db.commit()
            return
        effective_context = reel.enriched_context or reel.context
```

- [ ] **Step 4: Implement — wire retry in the except block**

Replace:

```python
    except Exception as exc:
        db.rollback()
        job = db.get(models.Job, job_id)
        if job:
            job.status = models.JobStatus.failed
            job.error = str(exc)[:2000]
            reel = db.get(models.Reel, job.reel_id) if job.reel_id else None
            if reel and reel.status.value == "generating":
                try:
                    transition(reel, "failed", REEL_TRANSITIONS)
                except ValueError:
                    pass
            db.commit()
        raise
    finally:
        db.close()
```

with:

```python
    except Exception as exc:
        db.rollback()
        if is_transient_error(exc) and self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * 2 ** self.request.retries)
        job = db.get(models.Job, job_id)
        if job:
            job.status = models.JobStatus.failed
            job.error = str(exc)[:2000]
            reel = db.get(models.Reel, job.reel_id) if job.reel_id else None
            if reel and reel.status.value == "generating":
                try:
                    transition(reel, "failed", REEL_TRANSITIONS)
                except ValueError:
                    pass
            db.commit()
        raise
    finally:
        db.close()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_generate_task.py -v`
Expected: both PASS.

- [ ] **Step 6: Run the full suite to check for regressions**

Run: `.venv/bin/pytest -q`
Expected: all tests pass (same count as before plus the 2 new ones).

- [ ] **Step 7: Commit**

```bash
git add worker/tasks/generate.py tests/test_generate_task.py
git commit -m "$(cat <<'EOF'
fix: generate_guide null-guards deleted reel + retries transient errors

max_retries=2 was declared but never used — self.retry() is now called
for network/IO failures (is_transient_error), with 30s/60s backoff.
Deterministic failures (bad JSON, quality gate) still fail once, same
as before. A reel deleted between job creation and pickup now fails
the job cleanly instead of raising AttributeError.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: `render_cut` — null-guard missing cut/reel + wire real retry

**Files:**
- Modify: `worker/tasks/render.py:1-16` (imports), `:35-53` (drop local `_heartbeat`, add null-guards), `:154-168` (except block)
- Test: `tests/test_render_task.py` (new)

**Interfaces:**
- Consumes: `worker.tasks.common.heartbeat` (aliased `_heartbeat`), `worker.tasks.common.is_transient_error`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_render_task.py
"""Task-level tests for render_cut: missing-row guards, transient-retry dispatch."""
from unittest.mock import MagicMock, patch

import httpx
import pytest
from celery.exceptions import Retry

from api import models


def _valid_guide_dict():
    beat = lambda i, t, v: {
        "index": i, "type": t, "duration_s": 5.0,
        "visual_direction": v, "on_screen_text": ["x"], "vo_script": "some words",
    }
    return {
        "platform": "youtube_shorts",
        "target_length_s": 15.0,
        "beats": [beat(0, "hook", "a"), beat(1, "body", "b"), beat(2, "cta", "c")],
        "caption": "Great reel",
        "hashtags": ["a", "b", "c", "d", "e"],
    }


def _make_job(job_id=1, cut_id=20):
    job = MagicMock()
    job.id = job_id
    job.cut_id = cut_id
    job.status = "pending"
    job.attempts = 0
    return job


def test_missing_cut_fails_job_cleanly():
    from worker.tasks.render import render_cut

    job = _make_job(cut_id=20)
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if model is models.Job else None

    with patch("worker.tasks.render.SessionLocal", return_value=db):
        render_cut(1)

    assert job.status == models.JobStatus.failed
    assert "20" in job.error


def test_missing_reel_fails_job_cleanly():
    from worker.tasks.render import render_cut

    job = _make_job(cut_id=20)
    cut = MagicMock()
    cut.reel_id = 99

    def _get(model, id_):
        if model is models.Job:
            return job
        if model is models.Cut:
            return cut
        return None  # Reel lookup returns None

    db = MagicMock()
    db.get.side_effect = _get

    with patch("worker.tasks.render.SessionLocal", return_value=db):
        render_cut(1)

    assert job.status == models.JobStatus.failed
    assert "99" in job.error


def test_transient_error_triggers_retry_with_backoff():
    from worker.tasks.render import render_cut

    job = _make_job(cut_id=20)
    cut = MagicMock()
    cut.reel_id = 99
    cut.guide = _valid_guide_dict()
    reel = MagicMock()
    reel.id = 99
    reel.voiceover_mode = "voiceover"

    def _get(model, id_):
        if model is models.Job:
            return job
        if model is models.Cut:
            return cut
        return reel

    db = MagicMock()
    db.get.side_effect = _get

    with (
        patch("worker.tasks.render.SessionLocal", return_value=db),
        patch("worker.tasks.render.get_asset_sourcer", side_effect=httpx.ConnectError("refused")),
        patch.object(render_cut, "retry", side_effect=Retry("retry")) as mock_retry,
    ):
        with pytest.raises(Retry):
            render_cut(1)

    mock_retry.assert_called_once()
    _, kwargs = mock_retry.call_args
    assert isinstance(kwargs["exc"], httpx.ConnectError)
    assert kwargs["countdown"] == 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_render_task.py -v`
Expected: `test_missing_cut_fails_job_cleanly` and `test_missing_reel_fails_job_cleanly` FAIL (`AttributeError` from unguarded `.reel_id` access). `test_transient_error_triggers_retry_with_backoff` FAILS (retry never invoked).

- [ ] **Step 3: Implement — imports, null-guards, drop local `_heartbeat`**

In `worker/tasks/render.py`, change:

```python
from engine.render.tts import get_tts_provider
from worker.celery_app import celery_app
```

to:

```python
from engine.render.tts import get_tts_provider
from worker.celery_app import celery_app
from worker.tasks.common import heartbeat as _heartbeat, is_transient_error
```

Delete the local `_heartbeat` definition:

```python
def _heartbeat(db, job, progress: int) -> None:
    job.progress = progress
    job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()


@celery_app.task(bind=True, max_retries=2)
def render_cut(self, job_id: int):
```

becomes:

```python
@celery_app.task(bind=True, max_retries=2)
def render_cut(self, job_id: int):
```

Add null-guards after the cut/reel fetch:

```python
        cut = db.get(models.Cut, job.cut_id)
        reel = db.get(models.Reel, cut.reel_id)
```

becomes:

```python
        cut = db.get(models.Cut, job.cut_id)
        if cut is None:
            job.status = models.JobStatus.failed
            job.error = f"Cut {job.cut_id} no longer exists"
            db.commit()
            return
        reel = db.get(models.Reel, cut.reel_id)
        if reel is None:
            job.status = models.JobStatus.failed
            job.error = f"Reel {cut.reel_id} no longer exists"
            db.commit()
            return
```

- [ ] **Step 4: Implement — wire retry in the except block**

Replace:

```python
    except Exception as exc:
        db.rollback()
        job = db.get(models.Job, job_id)
        if job:
            job.status = models.JobStatus.failed
            job.error = str(exc)[:2000]
            if job.cut_id:
                cut = db.get(models.Cut, job.cut_id)
                if cut and cut.status.value == "rendering":
                    try:
                        transition(cut, "failed", CUT_TRANSITIONS)
                    except ValueError:
                        pass
            db.commit()
        raise
    finally:
        db.close()
```

with:

```python
    except Exception as exc:
        db.rollback()
        if is_transient_error(exc) and self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=30 * 2 ** self.request.retries)
        job = db.get(models.Job, job_id)
        if job:
            job.status = models.JobStatus.failed
            job.error = str(exc)[:2000]
            if job.cut_id:
                cut = db.get(models.Cut, job.cut_id)
                if cut and cut.status.value == "rendering":
                    try:
                        transition(cut, "failed", CUT_TRANSITIONS)
                    except ValueError:
                        pass
            db.commit()
        raise
    finally:
        db.close()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_render_task.py -v`
Expected: all 3 PASS.

- [ ] **Step 6: Run the full suite to check for regressions**

Run: `.venv/bin/pytest -q`
Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add worker/tasks/render.py tests/test_render_task.py
git commit -m "$(cat <<'EOF'
fix: render_cut null-guards deleted cut/reel + retries transient errors

Same pattern as generate_guide (previous commit): wire max_retries=2
to an actual self.retry() call for network/IO failures, and fail
cleanly instead of raising AttributeError when the cut or reel was
deleted before the worker picked up the job.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: `enrich_context` — null-guard missing reel, dedupe heartbeat

**Files:**
- Modify: `worker/tasks/enrich_context.py:1-13` (imports), `:25-41` (drop local `_heartbeat`, add null-guard)
- Test: append to `tests/test_enrich_context_task.py`

**Interfaces:**
- Consumes: `worker.tasks.common.heartbeat` (aliased `_heartbeat`). No retry wiring here — `max_retries=0` is explicit/intentional per the spec, left unchanged.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_enrich_context_task.py`:

```python
def test_missing_reel_fails_job_cleanly():
    from worker.tasks.enrich_context import enrich_context
    from api import models

    job = _make_job()
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else None

    with patch("worker.tasks.enrich_context.SessionLocal", return_value=db):
        enrich_context(1)

    assert job.status == models.JobStatus.failed
    assert "10" in job.error
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_enrich_context_task.py::test_missing_reel_fails_job_cleanly -v`
Expected: FAIL with `AttributeError: 'NoneType' object has no attribute 'context'`.

- [ ] **Step 3: Implement — imports, null-guard, drop local `_heartbeat`**

In `worker/tasks/enrich_context.py`, change:

```python
from engine.observability import record_stage
from worker.celery_app import celery_app
from worker.tasks.generate import generate_guide
```

to:

```python
from engine.observability import record_stage
from worker.celery_app import celery_app
from worker.tasks.common import heartbeat as _heartbeat
from worker.tasks.generate import generate_guide
```

Delete the local `_heartbeat` definition:

```python
def _heartbeat(db, job, progress: int) -> None:
    job.progress = progress
    job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()


@celery_app.task(bind=True, max_retries=0)
def enrich_context(self, job_id: int):
```

becomes:

```python
@celery_app.task(bind=True, max_retries=0)
def enrich_context(self, job_id: int):
```

Add the null-guard right after the reel fetch:

```python
        reel = db.get(models.Reel, job.reel_id)

        job.status = models.JobStatus.running
```

becomes:

```python
        reel = db.get(models.Reel, job.reel_id)
        if reel is None:
            job.status = models.JobStatus.failed
            job.error = f"Reel {job.reel_id} no longer exists"
            db.commit()
            return

        job.status = models.JobStatus.running
```

Note: `enrich_context.py` no longer uses `datetime`/`timezone` directly except for `job.started_at = datetime.now(timezone.utc)` and `job.heartbeat_at = job.started_at` a few lines below — leave the `from datetime import datetime, timezone` import in place, it's still needed there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_enrich_context_task.py -v`
Expected: all tests (including the new one) PASS.

- [ ] **Step 5: Run the full suite to check for regressions**

Run: `.venv/bin/pytest -q`
Expected: all tests pass.

- [ ] **Step 6: Commit**

```bash
git add worker/tasks/enrich_context.py tests/test_enrich_context_task.py
git commit -m "$(cat <<'EOF'
fix: enrich_context null-guards a deleted reel before use

Same defensive pattern as generate_guide/render_cut. max_retries=0 is
intentional here (context-enrichment failures are not retried) and is
left unchanged.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Split `generate.py` into `visual_fallback.py` + `beat_enrichment.py`

**Files:**
- Create: `engine/generation/visual_fallback.py`
- Create: `engine/generation/beat_enrichment.py`
- Modify: `worker/tasks/generate.py` (remove moved code, import from new modules, update call sites)
- Modify: `tests/test_enrichment.py:126,148` (import path update)

**Interfaces:**
- Produces (from `visual_fallback.py`): `SECTION_FALLBACK_VISUALS: dict[str, str]`, `VO_TO_SHOT: list[tuple[str, str]]`, `fallback_visual(stub: BeatStub) -> str`, `VO_NAME_RE`, `NON_PERSON`, `NON_PERSON_PREFIXES`, `first_person(vo: str, existing: str) -> str`, `DEGENERATE_SUFFIXES`, `DEGENERATE_PHRASES`, `is_degenerate_visual(v: str) -> bool`.
- Produces (from `beat_enrichment.py`): `TACTICAL_MARKERS`, `is_shallow_beat(stub: BeatStub) -> bool`, `ENRICH_BATCH: int`, `enrich_batch(batch, context, llm) -> dict[int, str]`, `enrich_with_insight(stubs, context, llm) -> None`, `CONFLICT_RE`, `has_conflict_beat(stubs) -> bool`, `make_conflict_stub(context, llm, index) -> tuple[BeatStub, str] | None`.
- Consumes: `engine.generation.script_parser.BeatStub`, `.calc_duration`, `.derive_on_screen` (pre-existing).

This is a pure move-refactor — no behavior change. The existing tests in `tests/test_enrichment.py` (which exercise `_enrich_batch`/`_make_conflict_stub`'s prompt content) are the safety net; the "test" step here is running them against the new location instead of writing new ones.

- [ ] **Step 1: Confirm current baseline passes**

Run: `.venv/bin/pytest tests/test_enrichment.py -v`
Expected: all 15 tests PASS (this is the pre-refactor baseline).

- [ ] **Step 2: Create `engine/generation/visual_fallback.py`**

```python
import re

from engine.generation.script_parser import BeatStub


SECTION_FALLBACK_VISUALS: dict[str, str] = {
    "": "national team players celebrating trophy lift",
    "GOALKEEPER": "goalkeeper penalty save dramatic dive crowd reaction",
    "DEFENSE": "defender last-ditch tackle aerial duel clearance",
    "MIDFIELD": "midfielder pressing recovery run through-ball vision",
    "ATTACK": "striker one-on-one goal celebration sprint",
    "SECRET": "team tactical huddle training ground session",
    "ENDING": "team trophy lift celebration fans stadium",
}

VO_TO_SHOT: list[tuple[str, str]] = [
    ("penalt",      "penalty save dramatic dive"),
    ("save",        "reflex save goalkeeper fingertip"),
    ("tackle",      "crunching tackle last-ditch clearance"),
    ("press",       "high press recovery run intense"),
    ("intercept",   "interception reading play anticipation"),
    ("dribble",     "dribbling skill beat defender"),
    ("assist",      "key pass through-ball assist"),
    ("pass",        "vision through-ball creative passing"),
    ("goal",        "goal celebration strike finish"),
    ("finish",      "clinical finish one-on-one goal"),
    ("shoot",       "long-range strike shot on goal"),
    ("header",      "aerial header dominant set piece"),
    ("cross",       "cross delivery wide position"),
    ("sprint",      "explosive sprint pace recovery run"),
    ("defend",      "defensive positioning block clearance"),
    ("width",       "overlapping run wide position attack"),
    ("overlap",     "overlapping run cross delivery"),
    ("engine",      "box-to-box run defensive work rate"),
    ("architect",   "vision creative passing midfield"),
    ("glue",        "link play pressing combination midfield"),
    ("balance",     "defensive cover positioning wide"),
    ("iq",          "positional awareness anticipation reading game"),
    ("intelligent", "positional awareness anticipation reading game"),
    ("striker",     "striker movement clinical finish"),
    ("forward",     "forward run in behind goal"),
    ("winger",      "winger dribbling wide attack"),
    ("greatest",    "iconic career best moments highlight reel"),
    ("scar",        "decisive pressure match moment"),
    ("terrif",      "unstoppable attacking run danger"),
    ("depend",      "team relying on player decisive moment"),
    ("elevat",      "player raising teammates performance"),
    ("deadli",      "clinical striker finishing goal"),
    ("captain",     "captain armband leading team"),
    ("trophy",      "trophy lift celebration winners medal"),
]


def fallback_visual(stub: BeatStub) -> str:
    if stub.player:
        vo_lower = stub.vo_script.lower() if stub.vo_script else ""
        for keyword, shot in VO_TO_SHOT:
            if keyword in vo_lower:
                return f"{stub.player} {shot}"
        return f"{stub.player} match action highlight"
    return SECTION_FALLBACK_VISUALS.get(
        stub.section.upper(),
        f"football match {stub.section.lower()} intense action",
    )


VO_NAME_RE = re.compile(
    r'\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,}(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,})+)\b'
)
NON_PERSON = {"South American", "Premier League", "Copa America", "World Cup",
              "Champions League", "North American", "West European"}
NON_PERSON_PREFIXES = re.compile(
    r'\b(South|North|West|East|Premier|Copa|Champions|United|Real|Inter)\b'
)


def first_person(vo: str, existing: str) -> str:
    """Return the first plausible person name in `vo`, or `existing` if already known."""
    if existing:
        return existing
    for m in VO_NAME_RE.finditer(vo):
        name = m.group(1)
        if name not in NON_PERSON and not NON_PERSON_PREFIXES.search(name):
            return name
    return ""


DEGENERATE_SUFFIXES = (" footage", " highlights", " action shot", " close-up action shot")
DEGENERATE_PHRASES = ("playing football", "playing soccer", "show footage", "show highlights")


def is_degenerate_visual(v: str) -> bool:
    vl = v.strip().lower()
    if not vl or len(vl) < 10:
        return True
    if any(vl.endswith(s) for s in DEGENERATE_SUFFIXES):
        return True
    if any(p in vl for p in DEGENERATE_PHRASES):
        return True
    return False
```

- [ ] **Step 3: Create `engine/generation/beat_enrichment.py`**

```python
import json
import re

from engine.generation.script_parser import BeatStub, calc_duration, derive_on_screen


TACTICAL_MARKERS = re.compile(
    r"\b(allows?|enables?|because|which means|forces?|creates?|"
    r"press(?:ing)?|transition|high line|formation|shape|space|channel|"
    r"recover|position|structure|movement|role|system)\b",
    re.IGNORECASE,
)


def is_shallow_beat(stub: BeatStub) -> bool:
    return (
        stub.beat_type == "body"
        and bool(stub.player)
        and (
            len(stub.vo_script.split()) < 25
            or not TACTICAL_MARKERS.search(stub.vo_script)
        )
    )


ENRICH_BATCH = 3


def enrich_batch(batch: list[BeatStub], context: str, llm) -> dict[int, str]:
    beat_lines = "\n".join(
        f'  {{"index": {s.index}, "player": "{s.player}", "vo": "{s.vo_script[:100]}"}}'
        for s in batch
    )
    messages = [
        {"role": "system", "content":
            "You are a football tactical analyst. "
            "Return ONLY valid JSON — a list, one object per beat, "
            "each with 'index' (int) and 'tactical_sentence' (string)."},
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
    ]
    try:
        raw = llm.complete(messages, json_mode=True)
        data = json.loads(raw)
        if isinstance(data, dict):
            items = [data] if "index" in data else next(
                (v for v in data.values() if isinstance(v, list)), []
            )
        else:
            items = data
        return {item["index"]: item.get("tactical_sentence", "").strip() for item in items
                if isinstance(item, dict)}
    except Exception:
        return {}


def enrich_with_insight(stubs: list[BeatStub], context: str, llm) -> None:
    shallow = [s for s in stubs if is_shallow_beat(s)]
    if not shallow:
        return
    stub_map = {s.index: s for s in shallow}
    for i in range(0, len(shallow), ENRICH_BATCH):
        batch = shallow[i:i + ENRICH_BATCH]
        sentences = enrich_batch(batch, context, llm)
        for idx, sentence in sentences.items():
            stub = stub_map.get(idx)
            if stub and sentence and len(sentence.split()) >= 5:
                stub.vo_script = stub.vo_script.rstrip(" .") + ". " + sentence
                words = len(stub.vo_script.split())
                stub.duration_s = round(max(3.0, min(20.0, words / 2.3 + 1.0)), 1)


CONFLICT_RE = re.compile(
    r"\b(but|however|weakness|problem|concern|risk|challenge|"
    r"fragile|exposed|vulnerable|despite|worry|danger|question)\b",
    re.IGNORECASE,
)


def has_conflict_beat(stubs: list[BeatStub]) -> bool:
    return any(CONFLICT_RE.search(s.vo_script) for s in stubs if s.beat_type == "body")


def make_conflict_stub(context: str, llm, index: int) -> tuple[BeatStub, str] | None:
    messages = [
        {"role": "system", "content":
            "You are writing voiceover for a sports video. "
            "Return ONLY valid JSON with keys 'vo_script' and 'visual_direction'."},
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
    ]
    try:
        raw = llm.complete(messages, json_mode=True)
        data = json.loads(raw)
        vo = data.get("vo_script", "").strip()
        visual = data.get("visual_direction", "").strip()
        if not vo:
            return None
        stub = BeatStub(
            index=index, beat_type="body", section="CONFLICT",
            player="", vo_script=vo, duration_s=calc_duration(vo),
            on_screen_text=derive_on_screen(vo),
        )
        return stub, visual
    except Exception:
        return None
```

- [ ] **Step 4: Rewire `worker/tasks/generate.py` to use the new modules**

Change the top of the import block from:

```python
import json
import logging
import re as _re
from datetime import datetime, timezone

from pydantic import ValidationError

from api import models
from api.config import settings
from api.db import SessionLocal
from api.state import REEL_TRANSITIONS, transition
from engine.generation.evaluator import score_guide
from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
from engine.generation.llm import get_llm_provider, get_enrichment_provider, is_nvidia_generation
from engine.generation.llm_judge import judge_guide
from engine.generation.postprocess import clean_guide, _derive_on_screen as _postprocess_derive_on_screen
from engine.generation.prompt import build_messages, build_visuals_messages
from engine.generation.script_parser import BeatStub, calc_duration, derive_on_screen
from engine.generation import script_parser
from engine.observability import record_stage
from worker.celery_app import celery_app
from worker.tasks.common import heartbeat as _heartbeat, is_transient_error
```

(the `worker.tasks.common` import line was already added in Task 3 — this task only adds the two lines below it):

```python
from engine.generation.evaluator import score_guide
from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
from engine.generation.beat_enrichment import enrich_with_insight, has_conflict_beat, is_shallow_beat, make_conflict_stub
from engine.generation.llm import get_llm_provider, get_enrichment_provider, is_nvidia_generation
from engine.generation.llm_judge import judge_guide
from engine.generation.postprocess import clean_guide, _derive_on_screen as _postprocess_derive_on_screen
from engine.generation.prompt import build_messages, build_visuals_messages
from engine.generation.script_parser import BeatStub, calc_duration, derive_on_screen
from engine.generation import script_parser
from engine.generation.visual_fallback import fallback_visual, first_person, is_degenerate_visual
from engine.observability import record_stage
from worker.celery_app import celery_app
from worker.tasks.common import heartbeat as _heartbeat, is_transient_error
```

Also drop `import re as _re` (no longer used anywhere in this file once the moved code is deleted) and drop `calc_duration, derive_on_screen` from the `script_parser` import line since they're now only used inside `beat_enrichment.py` — check first whether `generate.py` still references them directly anywhere else; it doesn't (they were only used inside `_make_conflict_stub`, which just moved). So the line becomes:

```python
from engine.generation.script_parser import BeatStub
```

Delete these blocks entirely from `worker/tasks/generate.py` (all now live in the two new modules):
- `_SECTION_FALLBACK_VISUALS` dict
- `_VO_TO_SHOT` list
- `_fallback_visual()` function
- `_VO_NAME_RE`, `_NON_PERSON`, `_NON_PERSON_PREFIXES`
- `_first_person()` function
- `_DEGENERATE_SUFFIXES`, `_DEGENERATE_PHRASES`
- `_is_degenerate_visual()` function
- `_TACTICAL_MARKERS`
- `_is_shallow_beat()` function
- `_ENRICH_BATCH`
- `_enrich_batch()` function
- `_enrich_with_insight()` function
- `_CONFLICT_RE`
- `_has_conflict_beat()` function
- `_make_conflict_stub()` function

Update the four call sites that reference the old private names:

In `_stubs_to_platform_guide`, change:
```python
        if _is_degenerate_visual(vd):
            vd = _fallback_visual(s)
```
to:
```python
        if is_degenerate_visual(vd):
            vd = fallback_visual(s)
```

In `_generate_from_structured_script`, change:
```python
    with record_stage(db, reel.id, "enrich") as ev:
        shallow_before = sum(1 for s in stubs if _is_shallow_beat(s))
        _enrich_with_insight(stubs, context, enrichment_llm)
        ev.detail["shallow_beats"] = shallow_before
        ev.detail["enriched_beats"] = shallow_before - sum(1 for s in stubs if _is_shallow_beat(s))

    conflict_visual: dict[int, str] = {}
    if not _has_conflict_beat(stubs):
        result = _make_conflict_stub(context, enrichment_llm, index=len(stubs) - 1)
```
to:
```python
    with record_stage(db, reel.id, "enrich") as ev:
        shallow_before = sum(1 for s in stubs if is_shallow_beat(s))
        enrich_with_insight(stubs, context, enrichment_llm)
        ev.detail["shallow_beats"] = shallow_before
        ev.detail["enriched_beats"] = shallow_before - sum(1 for s in stubs if is_shallow_beat(s))

    conflict_visual: dict[int, str] = {}
    if not has_conflict_beat(stubs):
        result = make_conflict_stub(context, enrichment_llm, index=len(stubs) - 1)
```

Still in `_generate_from_structured_script`, change:
```python
    for s in stubs:
        if not s.player:
            s.player = _first_person(s.vo_script, "")
```
to:
```python
    for s in stubs:
        if not s.player:
            s.player = first_person(s.vo_script, "")
```

In `_enrich_standard_path_guide`, change:
```python
        stubs = [
            BeatStub(
                index=b.index,
                beat_type=b.type,
                section="",
                player=_first_person(b.visual_direction, ""),
                vo_script=b.vo_script,
                duration_s=b.duration_s,
                on_screen_text=b.on_screen_text[:],
            )
            for b in pg.beats
        ]
        _enrich_with_insight(stubs, context, enrichment_llm)
```
to:
```python
        stubs = [
            BeatStub(
                index=b.index,
                beat_type=b.type,
                section="",
                player=first_person(b.visual_direction, ""),
                vo_script=b.vo_script,
                duration_s=b.duration_s,
                on_screen_text=b.on_screen_text[:],
            )
            for b in pg.beats
        ]
        enrich_with_insight(stubs, context, enrichment_llm)
```

- [ ] **Step 5: Update `tests/test_enrichment.py` imports**

Change (line 126):
```python
    from worker.tasks.generate import _enrich_batch
```
to:
```python
    from engine.generation.beat_enrichment import enrich_batch as _enrich_batch
```

Change (line 148):
```python
    from worker.tasks.generate import _make_conflict_stub
```
to:
```python
    from engine.generation.beat_enrichment import make_conflict_stub as _make_conflict_stub
```

(Aliasing back to the old local names keeps the rest of those two test bodies — which call `_enrich_batch(...)` / `_make_conflict_stub(...)` — untouched.)

- [ ] **Step 6: Run tests to verify everything still passes**

Run: `.venv/bin/pytest tests/test_enrichment.py -v`
Expected: all 15 PASS, now importing from the new modules.

Run: `.venv/bin/pytest -q`
Expected: full suite passes, same total count as before this task (this is a pure move, no new tests).

Run: `wc -l worker/tasks/generate.py`
Expected: roughly 350-400 lines (down from 653).

- [ ] **Step 7: Commit**

```bash
git add engine/generation/visual_fallback.py engine/generation/beat_enrichment.py worker/tasks/generate.py tests/test_enrichment.py
git commit -m "$(cat <<'EOF'
refactor: split generate.py's content-generation helpers into engine/generation/

generate.py mixed Celery task orchestration with pure content logic
(visual fallback tables, name extraction, beat insight/conflict
enrichment) that has no dependency on db/Job/Celery. Moved to
visual_fallback.py and beat_enrichment.py, both now public modules
(dropped leading underscores). No behavior change — same tests,
new import paths.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Test backfill — `resolve_or_reuse` re-pin path

**Files:**
- Test: `tests/test_asset_sourcer.py` (new)

**Interfaces:**
- Consumes: `engine.render.asset_sourcer.resolve_or_reuse`, `.SourcedAsset`, `._fp` (all pre-existing).

- [ ] **Step 1: Write the tests**

```python
# tests/test_asset_sourcer.py
"""Tests for resolve_or_reuse's pin-and-reuse / re-pin-on-change behavior."""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api import models
from engine.render.asset_sourcer import SourcedAsset, _fp, resolve_or_reuse


class _FakeSourcer:
    def __init__(self):
        self.calls = 0

    def search(self, query, min_duration_s):
        self.calls += 1
        return SourcedAsset(
            source="pexels",
            source_ref=f"vid-{self.calls}",
            local_path=Path(f"/tmp/vid-{self.calls}.mp4"),
            license_str="pexels_free",
            duration_s=10.0,
            safe_to_publish=True,
        )


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _make_cut(db):
    reel = models.Reel(context="ctx")
    db.add(reel)
    db.flush()
    cut = models.Cut(reel_id=reel.id, platform=models.CutPlatform.youtube_shorts, target_length_s=30.0)
    db.add(cut)
    db.flush()
    return cut


def test_reuses_pinned_asset_when_direction_unchanged(db):
    cut = _make_cut(db)
    sourcer = _FakeSourcer()

    first = resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="player running", min_duration_s=5.0, sourcer=sourcer)
    second = resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="player running", min_duration_s=5.0, sourcer=sourcer)

    assert sourcer.calls == 1  # second call reused the pin — no new API call
    assert first[0][0].id == second[0][0].id


def test_repins_without_duplicating_when_direction_changes(db):
    cut = _make_cut(db)
    sourcer = _FakeSourcer()

    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="player running", min_duration_s=5.0, sourcer=sourcer)
    resolve_or_reuse(db, cut=cut, beat_index=0, visual_direction="player shooting", min_duration_s=5.0, sourcer=sourcer)

    assert sourcer.calls == 2
    pins = db.query(models.CutAsset).filter(
        models.CutAsset.cut_id == cut.id,
        models.CutAsset.beat_index == 0,
    ).all()
    assert len(pins) == 1  # stale pin replaced, not duplicated
    assert pins[0].resolved_from == _fp("player shooting")
```

- [ ] **Step 2: Run tests**

Run: `.venv/bin/pytest tests/test_asset_sourcer.py -v`
Expected: both PASS immediately — `resolve_or_reuse` already implements this behavior correctly; this task adds regression coverage, no production code change.

- [ ] **Step 3: Commit**

```bash
git add tests/test_asset_sourcer.py
git commit -m "$(cat <<'EOF'
test: cover resolve_or_reuse's pin-reuse and re-pin-on-change paths

Asset pinning is called out in CLAUDE.md as a hardening feature but
had no direct test. Covers both the fast path (identical
visual_direction reuses the pin, zero API calls) and the change path
(stale pin is replaced, not duplicated).

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Test backfill — `judge_guide` failure fallback

**Files:**
- Test: `tests/test_llm_judge.py` (new)

**Interfaces:**
- Consumes: `engine.generation.llm_judge.judge_guide`, `engine.generation.guide_schema.{Beat, PlatformGuide, MasterGuide}` (pre-existing).

- [ ] **Step 1: Write the tests**

```python
# tests/test_llm_judge.py
"""Tests for judge_guide's neutral-score fallback on LLM/parse failure."""
from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
from engine.generation.llm_judge import judge_guide


class _BrokenLLM:
    def complete(self, messages, **kwargs):
        raise RuntimeError("provider unreachable")


class _GarbageLLM:
    def complete(self, messages, **kwargs):
        return "not valid json"


def _make_guide() -> MasterGuide:
    beat = Beat(index=0, type="hook", duration_s=3.0, visual_direction="x", on_screen_text=["a"], vo_script="hi")
    pg = PlatformGuide(
        platform="youtube_shorts", target_length_s=30.0,
        beats=[beat, beat, beat], caption="c",
        hashtags=["a", "b", "c", "d", "e"],
    )
    return MasterGuide(title="t", niche="n", cuts=[pg])


def test_judge_guide_returns_neutral_score_when_llm_raises():
    score, issues = judge_guide("context", _make_guide(), _BrokenLLM())
    assert score == 50
    assert any("LLM judge unavailable" in i for i in issues)


def test_judge_guide_returns_neutral_score_on_malformed_json():
    score, issues = judge_guide("context", _make_guide(), _GarbageLLM())
    assert score == 50
    assert any("LLM judge unavailable" in i for i in issues)
```

- [ ] **Step 2: Run tests**

Run: `.venv/bin/pytest tests/test_llm_judge.py -v`
Expected: both PASS immediately — `judge_guide`'s existing `except Exception` already returns `(50, [...])`; this task adds regression coverage, no production code change.

- [ ] **Step 3: Commit**

```bash
git add tests/test_llm_judge.py
git commit -m "$(cat <<'EOF'
test: cover judge_guide's neutral-score fallback on LLM/parse failure

judge_guide silently returns (50, [...]) on any exception so
generation never blocks on judge availability — that fallback had no
test. Covers both a raising provider and a non-JSON response.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: Test backfill — TTS `synth_to_budget` rate-clamp math

**Files:**
- Test: `tests/test_tts.py` (new)

**Interfaces:**
- Consumes: `engine.render.tts.EdgeTTSProvider` (pre-existing).

- [ ] **Step 1: Write the tests**

```python
# tests/test_tts.py
"""Tests for EdgeTTSProvider.synth_to_budget's rate-clamp math."""
from unittest.mock import patch

from engine.render.tts import EdgeTTSProvider


def _tracking_provider(tmp_path):
    provider = EdgeTTSProvider(cache_dir=tmp_path)
    calls = []

    def fake_synthesize(text, rate="+0%"):
        calls.append(rate)
        return tmp_path / "out.mp3"

    return provider, calls, fake_synthesize


def test_synth_to_budget_clamps_positive_drift_to_25_percent(tmp_path):
    provider, calls, fake_synthesize = _tracking_provider(tmp_path)
    with (
        patch.object(provider, "synthesize", side_effect=fake_synthesize),
        patch("engine.render.tts._audio_duration", return_value=20.0),
    ):
        provider.synth_to_budget("some text", target_s=5.0)
    # drift = (20 - 5) / 5 = 3.0 -> 300% clamped to +25%
    assert calls == ["+0%", "+25%"]


def test_synth_to_budget_clamps_negative_drift_to_25_percent(tmp_path):
    provider, calls, fake_synthesize = _tracking_provider(tmp_path)
    with (
        patch.object(provider, "synthesize", side_effect=fake_synthesize),
        patch("engine.render.tts._audio_duration", return_value=1.0),
    ):
        provider.synth_to_budget("some text", target_s=10.0)
    # drift = (1 - 10) / 10 = -0.9 -> -90% clamped to -25%
    assert calls == ["+0%", "-25%"]


def test_synth_to_budget_skips_resynth_within_tolerance(tmp_path):
    provider, calls, fake_synthesize = _tracking_provider(tmp_path)
    with (
        patch.object(provider, "synthesize", side_effect=fake_synthesize),
        patch("engine.render.tts._audio_duration", return_value=5.5),
    ):
        provider.synth_to_budget("some text", target_s=5.0)
    # drift = (5.5 - 5) / 5 = 0.1, within default tol=0.15 -> no resynth
    assert calls == ["+0%"]
```

- [ ] **Step 2: Run tests**

Run: `.venv/bin/pytest tests/test_tts.py -v`
Expected: all 3 PASS immediately — `synth_to_budget`'s existing clamp math (`max(-25, min(25, int(drift * 100)))`) already behaves this way; this task adds regression coverage, no production code change.

- [ ] **Step 3: Commit**

```bash
git add tests/test_tts.py
git commit -m "$(cat <<'EOF'
test: cover synth_to_budget's +/-25% rate-clamp math

The speaking-rate clamp had no direct test. Covers the positive clamp
boundary, the negative clamp boundary, and the no-op path when
measured duration is already within the 15% tolerance.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10: Fix `CLAUDE.md` doc drift

**Files:**
- Modify: `/Users/luckyratanlaljain/project/reel-maker/CLAUDE.md:38,48,66-77,94-174,224`

**Interfaces:** none — docs only, no test.

- [ ] **Step 1: Fix the migration count comment**

Change (line 38):
```
.venv/bin/alembic upgrade head    # run migrations (0001 + 0002)
```
to:
```
.venv/bin/alembic upgrade head    # run migrations (0001-0003)
```

- [ ] **Step 2: Fix the test count comment**

Change (line 48):
```
.venv/bin/pytest                            # 68 tests across 5 files
```
to:
```
.venv/bin/pytest                            # 129 tests across 14 files
```

And (line 224):
```
- **Phase 3.5** ✅ — Hardening: acks_late reliability, idempotency guards, heartbeat + stuck-job reaper, per-beat asset pinning (deterministic re-render), observability (StageEvent), credential encryption, 68 tests across 5 files; evaluator upgraded to 17 axes (conversational tone, hook-CTA throughline, per-beat specificity, repetition)
```
to:
```
- **Phase 3.5** ✅ — Hardening: acks_late reliability, idempotency guards, heartbeat + stuck-job reaper, per-beat asset pinning (deterministic re-render), observability (StageEvent), credential encryption, 129 tests across 14 files; evaluator upgraded to 17 axes (conversational tone, hook-CTA throughline, per-beat specificity, repetition)
```

- [ ] **Step 3: Document the missing `enrich_context` pipeline stage**

Insert a new paragraph before the existing `**Guide generation — two paths:**` heading (currently line 66):

```
**Guide generation — two paths:**
```

becomes:

```
**Context enrichment stage (runs before generation):** `POST /api/reels` does not enqueue `generate_guide` directly — it creates the `Reel`/`Cut`/`Job` rows and enqueues `enrich_context` first. That task (`worker/tasks/enrich_context.py`) scores the raw context via `evaluate_context()`, and — unless the context is already a structured script (≥3 ALL-CAPS headers) or already scores above `ENRICH_THRESHOLD` — calls `llm_enrich()` to strengthen it, storing the result on `reel.enriched_context`. It then transitions the reel to `"generating"` and creates + enqueues the `generate_guide` job. `generate_guide` reads `reel.enriched_context or reel.context` (see `worker/tasks/generate.py`), so enrichment is transparent to the paths below.

**Guide generation — two paths:**
```

- [ ] **Step 4: Add the new modules to the Module layout tree**

In the `worker/` section (currently lines 110-118):
```
worker/
  celery_app.py       Celery instance; acks_late=True, beat schedule, split queues
  tasks/
    generate.py       generate_guide(job_id) — idempotency guard, heartbeat, enrichment,
                      conflict injection, visuals LLM, closed-loop eval retry, observability
    render.py         render_cut(job_id) — idempotency guard, heartbeat, resolve_or_reuse,
                      synth_to_budget, TTS-accurate timecodes, atomic MP4, observability
    noop.py           noop_job(job_id) — smoke test only
    maintenance.py    reap_stuck_jobs() — Celery beat task; fails stale running jobs
```
becomes:
```
worker/
  celery_app.py       Celery instance; acks_late=True, beat schedule, split queues
  tasks/
    common.py         heartbeat() + is_transient_error() — shared by all task modules below
    enrich_context.py enrich_context(job_id) — context quality score, optional LLM
                      enrichment, then transitions reel to generating and enqueues generate_guide
    generate.py       generate_guide(job_id) — idempotency guard, heartbeat, closed-loop
                      eval retry, retries transient errors, observability
    render.py         render_cut(job_id) — idempotency guard, heartbeat, resolve_or_reuse,
                      synth_to_budget, TTS-accurate timecodes, atomic MP4, retries
                      transient errors, observability
    noop.py           noop_job(job_id) — smoke test only
    maintenance.py    reap_stuck_jobs() — Celery beat task; fails stale running/enriching jobs
```

In the `engine/generation/` section (currently lines 122-130):
```
  generation/
    guide_schema.py   Beat, PlatformGuide, MasterGuide Pydantic models
    llm.py            LLMProvider + OllamaProvider; get_llm_provider(), get_enrichment_provider(),
                      is_nvidia_generation() — True when main LLM routes to NVIDIA NIM
    prompt.py         build_messages(prior_feedback=) + build_visuals_messages() — visuals system prompt anchors LLM to per-beat VO only
    script_parser.py  BeatStub + parse() — structured-script extractor
    evaluator.py      score_guide() — 17-axis rule scorer (0–100); see docs/evaluation.md
    llm_judge.py      judge_guide() — LLM semantic judge; 5 dims × 0–20 = 100 pts
    postprocess.py    clean_guide() — strips label prefixes; derives up to 5 on_screen_text segments
```
becomes:
```
  generation/
    guide_schema.py   Beat, PlatformGuide, MasterGuide Pydantic models
    llm.py            LLMProvider + OllamaProvider; get_llm_provider(), get_enrichment_provider(),
                      is_nvidia_generation() — True when main LLM routes to NVIDIA NIM
    context_enricher.py  evaluate_context() + llm_enrich() — pre-generation context quality gate
    visual_fallback.py   fallback_visual(), first_person(), is_degenerate_visual() — fallback
                      visuals when the LLM's visual_direction is missing or degenerate
    beat_enrichment.py   enrich_with_insight(), make_conflict_stub(), has_conflict_beat() —
                      structured-script beat-level tactical insight + conflict injection
    prompt.py         build_messages(prior_feedback=) + build_visuals_messages() — visuals system prompt anchors LLM to per-beat VO only
    script_parser.py  BeatStub + parse() — structured-script extractor
    evaluator.py      score_guide() — 17-axis rule scorer (0–100); see docs/evaluation.md
    llm_judge.py      judge_guide() — LLM semantic judge; 5 dims × 0–20 = 100 pts
    postprocess.py    clean_guide() — strips label prefixes; derives up to 5 on_screen_text segments
```

In the `migrations/versions/` section (currently lines 144-147):
```
migrations/
  versions/
    0001_initial.py   Original schema
    0002_improvements.py  Job heartbeat/meta, CutAsset pinning, Asset licensing, StageEvent table
```
becomes:
```
migrations/
  versions/
    0001_initial.py   Original schema
    0002_improvements.py  Job heartbeat/meta, CutAsset pinning, Asset licensing, StageEvent table
    0003_context_enrichment.py  Reel.enriched_context column, enriching ReelStatus + enrich JobType
```

In the `tests/` section (currently lines 149-156), add the two new test files created in Tasks 1-6 and correct existing counts to match the current suite:
```
tests/
  test_evaluator.py           28 tests — all 17 evaluator axes + helpers
  test_script_parser.py       11 tests — parse() routing, beat splitting, _derive_on_screen
  test_state.py               11 tests — REEL_TRANSITIONS, CUT_TRANSITIONS, invalid moves
  test_enrichment.py          15 tests — coerce_beat_type, enrich_batch response parsing, topic fence
  test_audio_text_sync.py     10 tests — clean_guide() regeneration, _build_text_filter() proportional timing, PATCH re-derivation, visual direction anchoring
  test_context_enricher.py    13 tests — evaluate_context axes, llm_enrich
  test_enrich_context_task.py  10 tests — idempotency, enrichment guard, structured script detection, missing-reel guard
  test_maintenance.py          4 tests — reaper reverts generating/enriching reels, ignores other statuses
  test_worker_common.py        15 tests — heartbeat(), is_transient_error() classification
  test_generate_task.py        2 tests — missing-reel guard, transient-error retry dispatch
  test_render_task.py          3 tests — missing-cut/reel guards, transient-error retry dispatch
  test_asset_sourcer.py         2 tests — resolve_or_reuse reuse + re-pin behavior
  test_llm_judge.py             2 tests — judge_guide neutral-score fallback
  test_tts.py                   3 tests — synth_to_budget rate-clamp math
```

- [ ] **Step 5: Verify the doc renders sensibly**

Run: `grep -n "enrich_context\|0003\|129 tests\|worker/tasks/common" CLAUDE.md`
Expected: all the new references show up.

No automated test for this task (docs only) — the check above is a manual sanity read.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs: fix CLAUDE.md drift — test/migration counts, missing enrich_context stage

CLAUDE.md said 68 tests / 5 files and "migrations 0001 + 0002" — both
stale. Bigger gap: the doc never mentioned that POST /api/reels routes
through enrich_context (context quality gate + optional LLM
enrichment) before generate_guide fires at all.

Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
EOF
)"
```

---

## Final Validation

- [ ] Run the full suite once more after all 10 tasks: `.venv/bin/pytest -q` — expect 129 tests total across 14 files, all passing (see Task 10 Step 4's table for the per-file breakdown).
- [ ] `wc -l worker/tasks/generate.py` — confirm the split reduced it from 653 lines to roughly 350-400.
- [ ] `grep -rn "_heartbeat(db, job" worker/tasks/*.py | wc -l` — confirm call sites are unchanged in count (still calling `_heartbeat(...)`, now via the aliased shared import) and `grep -c "^def _heartbeat" worker/tasks/*.py` returns 0 everywhere (no more local redefinitions).
