# System design: cut page does not poll while rendering/publishing

**Status:** Implemented. **Severity:** Low per `docs/roadmap.md`'s Open Issues table.

## 1. Problem

`docs/roadmap.md`'s Open Issues table: *"Cut page does not poll while rendering — `cut_card.html`
shows 'refresh to update' instead of an auto-refreshing fragment — same limitation for the
`'publishing'` status (Phase 4b)."*

`ui/templates/fragments/render_status.html` and `publish_status.html` are self-polling htmx
fragments (`hx-get=".../render-status?job_id=..." hx-trigger="load delay:3s" hx-swap="outerHTML"`)
that show a live progress bar and auto-refresh every 3s until the job reaches `done`/`failed`.
They're returned **directly** by `POST /api/cuts/{id}/render` and `POST /api/cuts/{id}/publish`, so
the operator sees live progress immediately after clicking the button, in that same page load.

The gap: `ui/templates/fragments/cut_card.html`'s `"rendering"`/`"publishing"` branches render a
static paragraph — *"Rendering in progress — refresh to update."* — with no htmx wiring at all.
This is the branch hit whenever the cut card is rendered **without** having just triggered the
job in that same request: a full page load of `GET /api/reels/{id}` (a fresh visit, a bookmark, a
second browser tab, or simply a page refresh) for a cut that's mid-render or mid-publish. The
operator has to manually reload the page repeatedly to see whether it's done.

## 2. Root cause

`Job` rows have a `cut_id` foreign key (`api/models.py`), and `trigger_render()`/`trigger_publish()`
(`api/routers/cuts.py`) create the Job row in the **same transaction** that sets `cut.status` to
`"rendering"`/`"publishing"` — but nothing on the read path (`GET /api/reels/{id}` → `reel_detail()`,
or any of the 4 call sites that return a fresh `cut_card.html` via the shared `_cut_card()` helper)
ever looks that Job row back up. `cut_card.html` only ever receives `cut` in its template context,
never the in-flight `Job`, so it has no way to embed the self-polling fragment — it can only fall
back to a static message.

## 3. Fix

`api/routers/cuts.py::active_job_for_cut(db, cut) -> Job | None` — the missing lookup:

```python
def active_job_for_cut(db, cut):
    job_type = {"rendering": JobType.render, "publishing": JobType.publish}.get(cut.status.value)
    if job_type is None:
        return None
    return (
        db.query(Job)
        .filter(Job.cut_id == cut.id, Job.type == job_type, Job.status.in_([pending, running]))
        .order_by(Job.created_at.desc())
        .first()
    )
```

Returns `None` for every status other than `"rendering"`/`"publishing"` (a cheap no-op for the vast
majority of calls), and defensively falls back to `None` if no matching row is found — not expected
in normal operation, since the 409 guards in `trigger_render()`/`trigger_publish()` (on `cut.status`
itself) ensure at most one render Job and one publish Job can ever be in flight per cut at a time,
and the Job row commits atomically with the status change.

Wired into every place a fresh `cut_card.html` can be rendered:

- `api/routers/reels.py::reel_detail()` (backing `GET /api/reels/{id}`, the actual page this
  roadmap item is about) precomputes `active_jobs = {cut.id: active_job_for_cut(db, cut) for cut in
  cuts}` and passes the dict; `reel.html`'s per-cut loop does
  `{% set active_job = active_jobs.get(cut.id) %}` immediately before including `cut_card.html`,
  matching how `cut` itself is already threaded through that same `{% include %}` via Jinja's
  default context-sharing. **This is the call site the fix actually exists for.**
- `api/routers/cuts.py::_cut_card()` (the shared helper behind `update_cut`, `approve_cut`,
  `choose_thumbnail`, `choose_hook_variant`) now takes `db` and passes
  `active_job_for_cut(db, cut)` too — but this is defensive consistency wiring, not the fix's real
  target: each of those four endpoints guards on `cut.status == "in_review"` and leaves it
  `in_review`/`approved`, states `active_job_for_cut()` returns `None` for without even querying.
  Found by review (see §7, Correction 1): with `SessionLocal`'s default `expire_on_commit=True`,
  `cut.status` is re-read fresh after each endpoint's `db.commit()` releases its row lock, so a
  concurrent `POST /render` or `/publish` landing in that narrow post-commit window would make
  `cut.status` (and therefore `active_job_for_cut()`) reflect the new `"rendering"`/`"publishing"`
  state by the time the response renders — an edge case, not the reason this wiring exists, but a
  real one it now also handles correctly rather than crashing or showing stale state.

`cut_card.html`'s `"rendering"`/`"publishing"` branches now do, e.g.:

```jinja
{% elif st == "rendering" %}
{% if active_job %}
{% set job = active_job %}
{% include "fragments/render_status.html" %}
{% else %}
<p>Rendering in progress — refresh to update.</p>
{% endif %}
```

`{% set job = active_job %}` aliases the value under the name `render_status.html`/
`publish_status.html` already expect (`job`) — these are the exact same, unmodified fragment
templates the trigger endpoints already return; nothing about their own htmx wiring, ids, or
downstream "Approve"/"Retry" buttons changes. Their root elements use distinct ids
(`render-status-{{ cut.id }}` / `publish-status-{{ cut.id }}`) from `cut_card.html`'s own
(`cut-card-{{ cut.id }}`, `render-section-{{ cut.id }}`), so nesting them inside the card creates no
duplicate-id conflict — the same non-collision property this codebase already documents for
`render_status.html`/`publish_status.html` versus their own parent `*-section-{{ cut.id }}`
containers (see `CLAUDE.md`'s HTMX fragment IDs convention).

## 4. Why this needed no schema change, no new endpoint, no polling-interval change

Every piece this fix needed already existed: `Job.cut_id`, the existing `GET /render-status` /
`GET /publish-status` endpoints the embedded fragments themselves call to keep polling, and the
existing `render_status.html`/`publish_status.html` templates. The gap was purely "the read path
never looks up the Job row the write path already created" — a lookup-and-wire fix, not a new
feature.

## 5. Testing

- `tests/test_cuts_publish_router.py` — 4 unit tests for `active_job_for_cut()` directly (real
  in-memory SQLite session, no HTTP layer): returns `None` for a non-rendering/publishing status;
  finds the pending render Job; ignores a `done`/stale Job row and falls back to `None` (the
  defensive branch); matches Job **type** to cut status correctly even when a live, non-terminal Job
  of the *other* type also exists for the same cut, with that other Job's `created_at` pinned
  strictly *later* so `order_by(created_at.desc())` alone (with the type filter removed) would
  pick the wrong one — both refinements from Correction 2 below.
- `tests/test_reels_router.py` — 4 integration tests through the real `GET /api/reels/{id}` route +
  real Jinja templates: a rendering cut with an active render Job embeds
  `/api/cuts/{id}/render-status?job_id={id}` and does **not** show the old static message; same for
  a publishing cut and the publish endpoint; a rendering cut with **no** matching Job row (the
  defensive fallback) still shows the static message rather than erroring or rendering blank; a
  reel with **two** rendering cuts, only one carrying a Job, proving no cross-cut leak (Correction 3
  below).
- Mutation-tested: reverted the router/template wiring (`ui/templates/fragments/cut_card.html`,
  `api/routers/cuts.py`, `api/routers/reels.py`, `ui/templates/reel.html`), confirmed both
  "embeds live status" tests fail for the exact predicted reason (static message present, live
  fragment markup absent), confirmed the fallback test still passes unaffected, then restored.
- Live browser verification (not just tests): seeded a throwaway SQLite DB with a `rendering`-status
  cut and a real `running` Job row, served it from a real `uvicorn` instance, loaded
  `GET /api/reels/{id}` in the browser pane. Confirmed on first paint — no manual refresh — the page
  showed the live progress bar ("running", "42%", "Fetching footage & synthesising audio…") instead
  of the static message, and confirmed via network-request inspection that htmx's own
  `hx-trigger="load delay:3s"` polling loop fired three real `GET .../render-status?job_id=...`
  requests over ~4 seconds — the actual auto-refresh behavior this roadmap item asked for, observed
  live, not just asserted in a test.

Full suite: 742 tests (was 734; +8), 1 deselected (golden), `ruff check --select F,E9 .` clean.

## 6. Corrections (from adversarial dual-lens review of the built code)

Two independent review passes (Lens A — Safety/State; Lens B — Contracts/Operations) ran against
the implemented diff before this shipped.

**Correction 1 — §3's "wired into every place" framing overstated the 4 `_cut_card()` POST sites'
real effect.** Both lenses independently traced `update_cut`/`approve_cut`/`choose_thumbnail`/
`choose_hook_variant`'s own status guards and found each only ever runs while `cut.status` is
`"in_review"` (leaving it `in_review` or `approved`) — never `"rendering"`/`"publishing"` — so
`active_job_for_cut()` returns `None` without even issuing a query at those 4 call sites in the
overwhelmingly common case. Lens B additionally found the one real exception: `SessionLocal`'s
default `expire_on_commit=True` means `cut.status` is re-read fresh after each endpoint's own
`db.commit()`, so a *concurrent* render/publish trigger landing in the narrow window between that
commit and the response being rendered would in fact be picked up correctly. §3 rewritten to state
plainly that `reel_detail()` is the call site this fix exists for, and the 4 POST sites are
defensive consistency wiring for that narrow race, not equally-load-bearing fix targets.

**Correction 2 — the Job-type-filter unit test was vacuous, twice over.** The first draft of
`test_active_job_for_cut_matches_job_type_to_cut_status` used a `done` stale render Job alongside a
`running` publish Job — Lens B pointed out the pre-existing `status.in_([pending, running])` filter
already excludes a `done` row on its own, so the test would pass identically with the `Job.type`
filter deleted; it was proving the status filter, not the type filter it's named for. Fixed by
giving the stale render Job a live (`pending`) status instead — then mutation-testing that fix
itself surfaced a **second**, independent way the test could still pass vacuously: both rows shared
the same default `created_at` instant (both committed in one transaction), and
`order_by(created_at.desc()).first()` happened to still return the correct row by coincidental
insertion-order timestamp proximity even with the `Job.type` filter removed entirely — confirmed by
actually deleting the filter and re-running the test, which still passed. Fixed by pinning the
render Job's `created_at` strictly *later* than the publish Job's, so a type-filter-removed query
would deterministically pick the wrong (render) row, and re-verified by mutation a second time: the
test now genuinely fails without the type filter, and passes with it.

**Correction 3 — no test proved cross-cut isolation in `reel.html`'s per-cut `active_job` threading.**
Lens A traced the Jinja scoping by hand and additionally rendered a real multi-cut page through
`TestClient` to confirm no leak — but the fix's own repo had no *permanent* regression test for it.
Added `test_reel_detail_only_the_rendering_cut_gets_a_live_status_fragment`: two cuts, both
`"rendering"`, only one with a matching Job row. A first draft of this test used one rendering cut
and one *draft* cut — mutation-tested against a deliberately-broken `reel.html` that threads a
single shared `active_job` value to every cut in the loop, and the test still passed, because a
`"draft"`-status cut never reaches the branch that reads `active_job` at all regardless of what
value it holds. Fixed by making the second cut `"rendering"` too (with no Job of its own) so the
leak has an actual branch to manifest in; re-verified by mutation that the corrected test now fails
against the shared-value bug (the job-less cut's card wrongly shows the other cut's `job_id`) and
passes against the real per-cut-keyed dict lookup.

**Confirmed, not changed:** both lenses independently verified the Job lookup is race-safe under
this codebase's actual concurrency model (a cut can never hold two non-terminal Jobs of the same
type — the 409 guards in `trigger_render()`/`trigger_publish()` and the reaper's resume mechanism,
which moves a `running` row back to `pending` in place rather than creating a second row, together
guarantee it); `active_job_for_cut()` and its call sites are pure reads with no interaction with the
`Job.claim_token` fencing mechanism (CLAUDE.md's Key conventions) — a resumed job simply shows as
`pending` in the embedded fragment, identical to what POST-driven polling already shows; the N+1
query pattern in `reel_detail()` (one query per cut) is not a real operational concern at this
codebase's actual scale (`CutPlatform` has 3 values, so at most 3 cuts per reel, and the query only
runs on a full page load, not on every 3-second poll).
