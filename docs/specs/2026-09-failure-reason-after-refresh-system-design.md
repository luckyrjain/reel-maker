# System design: failure reason is not shown after a page refresh

**Status:** Implemented. **Severity:** Low per `docs/roadmap.md`'s Open Issues table.

## 1. Problem

`docs/roadmap.md`'s Open Issues table: *"Failure reason is not shown after a page refresh —
`job.error` is rendered only in the polling fragment of the tab that started the job; the cut card
and reel page show a generic 'Failed'."*

`ui/templates/fragments/render_status.html`/`publish_status.html` show `{{ job.error }}` when a job
reaches `jstatus == "failed"` — but that's the fragment the *triggering* POST endpoint returns, in
the same tab, same page load. `cut_card.html`'s own `"failed"` branch never sees a `Job` at all: it
shows a single hard-coded message ("Failed. Retry render if the guide or footage needs
regenerating, or retry publish directly if the rendered video is fine.") regardless of what
actually went wrong — a bad LLM JSON schema, an ffmpeg crash, an expired OAuth token, a platform
API timeout. An operator debugging a failure has to have had the polling tab open at the moment it
failed, or dig through worker logs.

## 2. Fix

Closely mirrors the immediately-preceding fix for the "Cut page does not poll while rendering"
Open Issues item (`docs/specs/2026-09-cut-page-live-status-polling-system-design.md`), which
introduced `active_job_for_cut()` and the `active_job`/`active_jobs` threading pattern this reuses.

`api/routers/cuts.py::latest_failed_job_for_cut(db, cut) -> Job | None`:

```python
def latest_failed_job_for_cut(db, cut):
    if cut.status.value != "failed":
        return None
    return (
        db.query(Job)
        .filter(Job.cut_id == cut.id, Job.type.in_([render, publish]), Job.status == failed)
        .order_by(Job.created_at.desc())
        .first()
    )
```

**Deliberately not type-filtered**, unlike `active_job_for_cut()`: `CUT_TRANSITIONS["failed"]`
(`api/state.py`) is reachable from either a failed render *or* a failed publish — a cut can fail
rendering, get retried, and fail publishing instead — so the row that actually explains the current
`"failed"` state is whichever type failed **most recently**, not a fixed type derived from
`cut.status` the way `"rendering"`→render/`"publishing"`→publish is. `job_task`'s own failure path
(`worker/tasks/common.py`) sets `cut.status` to `"failed"` in the same transaction it fails the Job
row, so the most-recently-failed row for this cut is always the one that caused it.

Wired the same way as the render/publish-status fix: `reel_detail()` precomputes
`failed_jobs = {cut.id: latest_failed_job_for_cut(db, cut) for cut in cuts}`, `reel.html` threads
`failed_job = failed_jobs.get(cut.id)` per cut before including `cut_card.html`, and `_cut_card()`
(the shared helper behind the 4 POST endpoints) does the same lookup directly. As with the sibling
fix, `reel_detail()` is the call site this fix actually exists for — `update_cut`/`approve_cut`/
`choose_thumbnail`/`choose_hook_variant` (the 4 `_cut_card()` callers) each guard on
`cut.status == "in_review"` and never actually reach `"failed"` in the ordinary case, so the
`_cut_card()` wiring is the same kind of narrow-race defensive consistency the sibling fix's own
Correction 1 already established, not a second load-bearing call site. `cut_card.html`'s
`"failed"` branch gains one line before its existing generic message:

```jinja
{% if failed_job and failed_job.error %}
<p class="error" style="margin-top:10px;margin-bottom:4px;">{{ failed_job.error }}</p>
{% endif %}
```

`job.error` is already sanitised server-side before it's ever written (`_error_text` in
`worker/tasks/common.py` — NUL bytes stripped, lone surrogates replaced, `[parameters: …]` redacted)
and Jinja's default autoescaping handles the rest; this is the identical display pattern
`render_status.html`/`publish_status.html` already use, just reached from a different context.

## 3. Extension found during review: the reel level has the identical gap

The roadmap entry's own wording — *"the cut card **and reel page** show a generic 'Failed'"* —
already implied this, and Lens B's review confirmed it as a real, unaddressed gap in the first
version of this fix: an `enrich`/`generate` job failure rolls the *reel* (not a cut) back to
`"failed"` via `api/state.py::JOB_IN_FLIGHT`'s `rollback_owner()` — the exact same mechanism that
rolls a cut back on a render/publish failure. `reel.html`'s own status badge
(`<span class="badge badge-{{ reel.status.value }}">`) showed a bare `"failed"` with no reason at
all — not even a generic message, since there's no per-reel equivalent of `cut_card.html`'s
hard-coded "Failed. Retry render..." text. `pipeline_status.html` (the fragment that *does* show
`job.error` for enrich/generate jobs) is only ever returned by `POST /api/reels` and
`GET /api/reels/{id}/active-job-fragment` directly — `reel.html` never includes it.

Fixed with the direct reel-level sibling, `api/routers/reels.py::latest_failed_job_for_reel(db,
reel) -> Job | None` — same shape as `latest_failed_job_for_cut()` (not type-filtered, for the
identical reason: a reel can fail either enrichment or generation, and the most-recently-failed row
of either type is the one that explains the current state), wired into `reel_detail()` as
`failed_reel_job` and rendered directly in `reel.html` next to the status badge:

```jinja
{% if reel.status.value == "failed" and failed_reel_job and failed_reel_job.error %}
<p class="error" style="margin-top:6px;">{{ failed_reel_job.error }}</p>
{% endif %}
```

Both `latest_failed_job_for_cut()` and `latest_failed_job_for_reel()` order by
`created_at.desc(), id.desc()` (the `id` tie-break was Lens A's finding, below) rather than
`created_at.desc()` alone.

## 4. Testing

- `tests/test_cuts_publish_router.py` — 5 new unit tests for `latest_failed_job_for_cut()`: `None`
  for a non-`"failed"` status; the defensive `None` fallback with no matching row; finds a failed
  render Job; is genuinely type-agnostic (picks the most recent of a failed render *or* failed
  publish row, verified with the wrong-type row pinned to a later `created_at` so ordering alone,
  without any type constraint to remove, still has to be exercised correctly — there's no type
  filter here to mutate out, so this test instead proves the function returns the right row across
  types, matching the design's actual "most recent regardless of type" contract); ignores a
  non-terminal (`pending`/`running`) Job for a `"failed"` cut.
- `tests/test_reels_router.py` — 3 new cut-level integration tests through the real
  `GET /api/reels/{id}` route: a failed cut with a failed Job's `error` set shows that actual text;
  a failed cut with no matching Job row shows only the pre-existing generic message (the defensive
  fallback); a multi-cut reel with two independently-failed cuts shows each its own error, never
  the other's. Plus, for the reel-level extension (§3): 3 unit tests for
  `latest_failed_job_for_reel()` mirroring the cut-level ones (non-`"failed"` status, defensive
  fallback, type-agnostic most-recent-wins), and 2 integration tests (a failed reel's actual error
  text renders; a failed reel with no matching Job row shows no stray error paragraph).
- Mutation-tested: reverted the cut-level router/template wiring, confirmed both non-fallback
  integration tests fail for the exact predicted reason while the fallback test is unaffected, then
  restored; separately mutation-tested `latest_failed_job_for_cut()`'s `order_by()` (removing it
  makes the type-agnostic-most-recent test fail, since the database then returns whichever row it
  wants, not necessarily the most recent), then restored. Repeated both mutation passes for the
  reel-level extension: reverted `latest_failed_job_for_reel()` and its `reel.html` display line
  separately (the former via a full stash-and-restore of `api/routers/reels.py`, the latter with a
  scoped removal of just the display block, since `latest_failed_job_for_cut()`'s own display line
  had already been proven load-bearing by the cut-level mutation test and re-proving the identical
  property for its sibling with a full-file revert would have been redundant) — both failed for the
  right reason, both restored.
- Live browser verification, both levels: seeded a throwaway SQLite DB with a `"failed"` cut and a
  real `failed` Job carrying a specific ffmpeg error string, served from a real `uvicorn` instance,
  confirmed the exact error text rendered on first paint of `GET /api/reels/{id}`; repeated with a
  `"failed"` reel and a real `failed` `generate`-type Job, confirmed the reel-level error text
  rendered next to the status badge — not just asserted in a test, either time.

Full suite: 755 tests (was 742; +13), 1 deselected (golden), `ruff check --select F,E9 .` clean.

## 5. Corrections (from adversarial dual-lens review of the built code)

**Correction 1 (Lens A) — ordering tie-break added as hardening, not a live bug.** `order_by()` was
`created_at.desc()` alone in the first draft. Lens A traced every path that could theoretically
produce a `created_at` tie or inversion (retries reuse the same Job row and never touch
`created_at`; the reaper's resume mechanism only touches `status`/`error`/`reaper_resumes`; a new
Job row is only ever created by `trigger_render()`/`trigger_publish()`, which cannot happen while
the cut is still `"failed"`) and confirmed the ordering is already correct in every reachable case
under this codebase's actual concurrency model — this was **not** a live bug. Added
`, Job.id.desc()` as a tie-break anyway, since it's free and removes a dependency on wall-clock
`created_at` values assigned by the API process rather than the database, matching the same
belt-and-suspenders reasoning CLAUDE.md documents elsewhere in this codebase (e.g. the
`Job.claim_token` fencing mechanism) for not relying on a single signal when a second, free one is
available.

**Correction 2 (Lens B) — the reel-level extension in §3 above.** Found as a real, previously-missed
gap: the design doc's first draft, and the code, addressed only the cut level, even though the
roadmap entry's own wording named the reel page too and the underlying mechanism
(`JOB_IN_FLIGHT`/`rollback_owner()`) is identical for both. Closed before merge — see §3.

**Confirmed, not changed:** both lenses independently verified `latest_failed_job_for_cut()`'s
`error` is never stale or empty for a row this function can return — every code path that sets a
Job to `failed` sets `error` in the same write, and a job that eventually succeeds after a
transient-retry reset ends up `done` (excluded by the `status == failed` filter) rather than
`failed` with a leftover message; both functions and their call sites are pure reads with no
commit/mutation and no lock-escalation risk (the 4 `_cut_card()` callers all query after their own
`db.commit()` releases the row lock); the type-agnostic unit tests are not vacuous (verified by
mutation, both at the cut and reel level).
