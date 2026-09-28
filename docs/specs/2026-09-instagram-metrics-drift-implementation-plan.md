# Implementation plan: Instagram Insights metric names may drift

`plan_id`: `PLANSET-9fbeefce7412c5ec-01`
`readiness`: READY
`criticality`: Medium
Design: `docs/specs/2026-09-instagram-metrics-drift-system-design.md`
Change impact: `docs/specs/2026-09-instagram-metrics-drift-change-impact-report.md`
Executor: `loop-task-implementer`

## Execution waves

**Wave 1** (no dependencies, parallel-safe): T1, T2
**Wave 2** (depends on wave 1): T3 (depends on T1), T4 (depends on T2)
**Wave 3** (depends on wave 2): T5
**Wave 4** (separate, post-build): T6 — run after the Builder pass, not part of it

---

### T1 — `InstagramMetricsFetcher.fetch()`: allowlist diff + `record_stage` wrap

**File:** `engine/publish/metrics.py`
**Dependencies:** none

Implement, in `InstagramMetricsFetcher.fetch()`:
1. Wrap the existing `httpx.get(...)` call through the `by_name` construction in
   `record_stage(db, cut.reel_id, "instagram_metrics", cut_id=cut.id)` — following this codebase's
   documented composition rule (CLAUDE.md's `record_stage()` entry) for a call that must never fail
   its caller but must still tell the truth.
2. Compute `returned_names = {d["name"] for d in data}` from the **raw** response, before building
   `by_name` — not from `by_name.keys()`. Diff against the requested metric names
   (`set(self._METRICS.split(","))`).
3. If `missing := requested - returned_names` is non-empty: set `ev.ok = False`,
   `ev.detail["missing_metrics"] = sorted(missing)`, and continue — return the (possibly partial)
   `EngagementMetrics` normally, do **not** raise.
4. If `resp.raise_for_status()` raises (whole-request failure): let it propagate through
   `record_stage`'s own exception path unchanged (which already sets `ev.ok=False`/
   `ev.detail["error"]` and re-raises) — no new exception handling needed here, just make sure the
   raising call is *inside* the `with record_stage(...)` block.
5. The existing `if not data: return None` early-return (fully-empty response, "no insights yet")
   stays **outside**/**before** the new check — no `StageEvent` write for that case (per design §3's
   explicit exemption). Verify this by placement: the `record_stage` wrap should start after this
   early return, not before it — otherwise every just-published video would emit a spurious
   `ok=False` "missing everything" event on its very first pull.

**Verification:**
- `.venv/bin/pytest tests/test_metrics_fetcher.py -k instagram -v`
- Manually trace: does the wrap correctly leave `test_instagram_fetch_returns_none_when_no_data`
  (the `data: []` case) untouched — no `record_stage` call at all? This is review_trigger #3's
  placement concern from the change-impact report; get this right at implementation time; not to be
  raised by review, though a reviewer should still check it.

### T2 — `_pipeline_summary()`: exclude post-publish stages from headline totals

**File:** `api/routers/reels.py`
**Dependencies:** none

1. Add a module-level constant near `_pipeline_summary`, e.g.:
   `_EXCLUDED_FROM_TOTALS = {"instagram_metrics"}` — named so a future post-publish stage is an
   obvious one-line addition (per design §8).
2. In `_pipeline_summary()`, change `total_cost = sum(e.cost_usd or 0.0 for e in stage_events)` and
   `total_latency_ms = sum(e.latency_ms or 0 for e in stage_events)` to skip
   `e.stage in _EXCLUDED_FROM_TOTALS`.
3. Leave the `stage_summary` loop (`for e in stage_events: ...`) **completely unfiltered** — every
   stage, excluded or not, still gets its own row with its own `count`/`latency_ms`/`cost_usd`/
   `failures`. This is review_trigger #2 from the change-impact report — get the filter scoped to
   exactly the two sum computations, nowhere else.

**Verification:**
- `.venv/bin/pytest tests/test_reels_router.py -k pipeline_summary -v`

### T3 — Tests: `engine/publish/metrics.py`'s new behavior

**File:** `tests/test_metrics_fetcher.py`
**Dependencies:** T1

Add, in the existing `# ── Instagram ──` section, alongside the existing tests (all-`MagicMock`
style, matching this file's convention — no real DB session needed since `record_stage` can be
patched directly):

1. `test_instagram_fetch_records_a_clean_pull_as_ok` — complete response (all 3 metrics), patch
   `engine.publish.metrics.record_stage`, assert the yielded `ev` (or the mock's call) reflects
   `ok=True` and no `missing_metrics` key set.
2. `test_instagram_fetch_records_missing_metrics_without_raising` — a response missing one
   requested metric entirely (e.g. only `plays`+`likes`, no `comments` entry in `data`). Assert:
   (a) `fetch()` still returns a partial `EngagementMetrics` (existing merge-friendly contract,
   `comments=None`, others populated), (b) `record_stage`'s `ev.ok == False` and
   `ev.detail["missing_metrics"] == ["comments"]`.
3. `test_instagram_fetch_does_not_flag_a_present_but_valueless_metric_as_missing` — **the exact
   regression the design's own revision note exists to guard against**: reuse the existing
   `test_instagram_fetch_ignores_metrics_without_values` fixture shape (`{"name": "plays", "values":
   []}`, and only that one entry in `data`) and assert `"plays"` does **not** appear in
   `missing_metrics` (only `"likes"`/`"comments"`, which are genuinely absent from `data`, should).
   Mutation-test this one yourself before considering it done: temporarily change the
   implementation to diff against `by_name.keys()` instead of the raw `data` names, confirm this
   test fails, then revert.
4. `test_instagram_fetch_records_a_whole_request_failure` — `resp.raise_for_status()` raises (reuse
   the existing pattern from other tests that mock a raising response). Assert the exception still
   propagates (unchanged contract — `pytest.raises(...)`) AND that a `StageEvent` was actually
   persisted with `ok=False` and a real `detail["error"]` string. Recommend using a real in-memory
   SQLite session here (see `tests/test_tasks_real_db.py` for this repo's existing pattern of using
   a real DB when the property under test is "a row was actually written," not just "a function was
   called") rather than mocking `record_stage` itself, per the change-impact report's test-strength
   recommendation.
5. Confirm the existing `test_instagram_fetch_parses_insights`,
   `test_instagram_fetch_returns_none_when_no_data`, and
   `test_instagram_fetch_ignores_metrics_without_values` all still pass unmodified (or with only an
   added, non-breaking assertion) — do not weaken any existing assertion to make the new code fit.

**Verification:** `.venv/bin/pytest tests/test_metrics_fetcher.py -v`

### T4 — Tests: `_pipeline_summary()`'s exclusion behavior

**File:** `tests/test_reels_router.py`
**Dependencies:** T2

Add `test_pipeline_summary_excludes_instagram_metrics_from_headline_totals_but_not_the_stage_table`
next to the existing `test_pipeline_summary_aggregates_stages_and_failures` (same real-in-memory-
SQLite style): insert one `StageEvent(stage="instagram_metrics", cost_usd=..., latency_ms=...,
ok=False)` alongside an ordinary stage (e.g. `"generate"`) with known cost/latency, call
`_pipeline_summary`, and assert:
- `stage_summary["instagram_metrics"]` exists with the correct `count`/`latency_ms`/`cost_usd`/
  `failures` (proving the per-stage table is unfiltered).
- `total_cost`/`total_latency_ms` equal the `"generate"` row's values alone (proving the
  `instagram_metrics` row's cost/latency were excluded from the sums).

**Mutation-test this yourself**: temporarily remove the exclusion-set check from `_pipeline_summary`
(T2's change), confirm this new test fails (the totals would then include the excluded stage's
values), then restore. This is the change-impact report's explicitly-named regression guard for the
round-1 review's headline-stat finding — do not skip the mutation verification.

**Verification:** `.venv/bin/pytest tests/test_reels_router.py -v`

### T5 — Documentation

**Files:** `CLAUDE.md`, `docs/roadmap.md`
**Dependencies:** T1, T2, T3, T4

1. `CLAUDE.md`: add a Key-conventions entry (or extend the existing `record_stage()`/
   `_pipeline_summary` module-layout lines) documenting: the new `instagram_metrics` stage, the
   `by_name` vs. raw-`data`-names distinction (this is a subtle, easy-to-regress detail worth a
   permanent note, same treatment this file already gives the reaper-resume fencing-token gotchas),
   and the `_EXCLUDED_FROM_TOTALS` exclusion set's purpose.
2. `docs/roadmap.md`: mark the "Instagram Insights metric names may drift" row `✅ Fixed`, describing
   the two failure modes closed and the review-round finding (headline-stat mislabeling) the same
   way prior rows in this table describe their own review history — this table's established voice
   is "what was broken, what closes it, what a review round caught," not just "done."
3. Update test count comments in CLAUDE.md's `Commands`/module-layout sections to reflect the new
   test count (run `.venv/bin/pytest --collect-only -q` after T1-T4 land to get the exact number).

**Verification:** re-read both files for consistency with the actual merged code; no test runs
against a docs-only change.

---

### T6 — Independent review (post-build, not part of the Builder pass)

Runs separately after T1-T5 are built and pushed, same as this pipeline's prior features: dispatch
independent reviewer(s) against the built diff (dual-lens minimum — Safety/State and
Contracts/Operations — plus this feature's own three change-impact `review_triggers`, which a
reviewer should re-verify directly against the merged code, not trust the design/plan's own claims).

## Traceability

| Task | Design section | Change-impact review_trigger |
|---|---|---|
| T1 | §2, §3, §4, §7 | #1 (raw `data` names, not `by_name`), #3 (record_stage composition) |
| T2 | §2, §7, §8 | #2 (exclusion scoped to totals only) |
| T3 | §3, §10 | #1, #3 |
| T4 | §8 | #2 |
| T5 | (all) | — |
