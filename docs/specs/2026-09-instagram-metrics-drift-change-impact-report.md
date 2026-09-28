# Change impact report: Instagram Insights metric names may drift

**Assessment target:** `docs/specs/2026-09-instagram-metrics-drift-system-design.md` (revised once,
Ready for implementation) — `docs/roadmap.md` Open Issues, Medium severity.
**Coverage status:** COMPLETE (repository read available; every impacted call site and existing
test grepped and read directly).
**Criticality:** Medium (matches the roadmap item's own severity — a silent, permanent analytics
data gap, not a crash or a safety/security issue).

## Impacted repositories

Single repo: `reel-maker`. No cross-repo dependency.

## Change classes

- **Behavior change** to `engine/publish/metrics.py::InstagramMetricsFetcher.fetch()` — adds an
  allowlist diff against the raw Graph API response and a `record_stage(...)` wrapper. Return type
  (`EngagementMetrics | None`) and calling convention (`fetch(cut, credential, db)`) are unchanged.
- **Behavior change** to `api/routers/reels.py::_pipeline_summary()` — adds a stage-name exclusion
  set (`_EXCLUDED_FROM_TOTALS = {"instagram_metrics"}` or equivalent) applied only to the two
  headline `total_cost`/`total_latency_ms` sums. Return dict shape and every existing key are
  unchanged; `stage_summary` remains unfiltered (every stage still gets a row, per the design).
- **No** schema migration, **no** new/changed HTTP endpoint, **no** template change, **no** config
  field.

## Impacted services / contracts

- **Instagram Graph API** (external, read-only `GET /{media-id}/insights?metric=...`) — unchanged
  request shape; only response parsing is affected.
- **`MetricsFetcher` interface** (`engine/publish/metrics.py`) — contract unchanged.
  `YouTubeMetricsFetcher` is untouched (confirmed by grep: no shared helper is being extracted that
  would also change its behavior).
- **`worker/tasks/metrics.py::_pull_one()`** — caller of `fetch()`, unaffected: still receives an
  `EngagementMetrics | None` with the same partial-merge semantics; the new `record_stage` write
  happens *inside* `fetch()`, invisible to this caller's control flow. `pull_publish_metrics()`'s
  per-cut `try/except Exception` isolation is unchanged and still the outer safety net for mode-2
  (whole-request) failures.
- **`api/routers/reels.py`'s `GET /api/reels/{id}` route** — consumer of `_pipeline_summary()`'s
  return dict via `reel.html`. No key added/removed/renamed in that dict; only the *values* of
  `total_cost`/`total_latency_ms` change (now excluding `instagram_metrics` rows), and only once
  such rows start existing (zero behavior change until the first Instagram metrics pull after
  deploy).

## Impacted data

- **`stage_events` table** — new, indefinitely-growing category of rows (`stage="instagram_metrics"`),
  one per Instagram cut per 6-hour `pull_publish_metrics()` tick, for as long as that cut stays
  `published`. No schema change; `stage` is an unconstrained `String(50)`. This is the single
  largest operational-impact item in this change — see Operational impacts below.
- **`cuts` table** — no new column; `cut.views`/`likes`/`comments`/`metrics_updated_at` update
  behavior is unchanged (still written by `_pull_one()`, not touched by this fix).

## Impacted dependencies

None new. `httpx` (existing), `engine.observability.record_stage` (existing, reused as-is).

## Impacted owners

Single-maintainer repo per prior features in this pipeline; no cross-team review trigger.

## Required tests

1. **`tests/test_metrics_fetcher.py`** (existing file, all-`MagicMock`-based style — confirmed by
   reading it in full):
   - Complete-response case (existing `test_instagram_fetch_parses_insights`) must still return the
     same `EngagementMetrics`, and should gain an assertion that `record_stage` was invoked with
     `ok=True` / no `missing_metrics` (patch `engine.publish.metrics.record_stage` directly, matching
     this file's existing all-mock convention — do **not** need a real DB session here).
   - **New**: a response missing an entire requested metric (e.g. only `plays`+`likes` returned,
     `comments` absent from `data`) must still return the partial `EngagementMetrics` (existing
     partial-merge-friendly behavior preserved) AND must produce a `record_stage` call with
     `ok=False`, `detail["missing_metrics"] == ["comments"]`.
   - **New, the exact case the round-1 review flagged**: a response where a requested metric IS
     present in `data` by name but with an **empty `values` list** (the existing
     `test_instagram_fetch_ignores_metrics_without_values` fixture: `{"name": "plays", "values":
     []}`) must be treated as returned-by-name, not missing — confirmed by re-reading this exact
     existing test against the design's own worked example in §3. A new assertion should verify this
     case does NOT appear in `missing_metrics` (distinguishing "renamed/dropped" from "present but
     valueless"), otherwise the fix would inherit the exact truthiness bug the design's revision
     exists to avoid.
   - **New**: a whole-request HTTP failure (`resp.raise_for_status()` raises) must still propagate
     the exception unchanged (so `_pull_one()`'s existing per-cut catch is unaffected) AND must
     produce a `record_stage` call that ends with `ok=False`/`detail["error"]` set — verifiable via
     `record_stage`'s real (unmocked) implementation writing to a real in-memory-SQLite session, or
     by asserting the context manager's own exception-path behavior if `record_stage` itself is
     mocked (the former gives stronger, non-tautological coverage — recommend it, matching this
     repo's general preference for exercising real code where feasible per CLAUDE.md's own testing
     conventions).
2. **`tests/test_reels_router.py`** (existing file, real in-memory-SQLite session per
   `test_pipeline_summary_aggregates_stages_and_failures`):
   - **New**: a `StageEvent(stage="instagram_metrics", cost_usd=..., latency_ms=..., ok=False)` row
     must still appear in `stage_summary["instagram_metrics"]` with its own `count`/`latency_ms`/
     `cost_usd`/`failures` (proving the per-stage table stays unfiltered), while `total_cost`/
     `total_latency_ms` must NOT include its values (proving the exclusion set actually excludes).
     This is the single test that would have caught the round-1 review's headline-stat gap had it
     existed before the fix — write it as a genuine regression guard (mutation-test it against a
     version of `_pipeline_summary` with no exclusion set, confirm it fails), not just a
     confirmation of intended behavior.
3. No change needed to `tests/test_metrics_task.py` (worker orchestration) or
   `tests/test_publish_registry.py` (fetcher-selection mapping) — neither's existing assertions
   touch the parsing/instrumentation internals this fix changes; confirmed by reading both files.

## Operational impacts

- **Unbounded `stage_events` growth per long-lived published Instagram cut** (see Impacted data
  above) — the design's own §6/§8 already call this out and address the headline-stat mislabeling
  it would otherwise cause, but this report flags it again as the one item worth a human decision
  before merge, not just noting: is indefinite accumulation in this table acceptable, or does it
  warrant a retention/pruning policy (e.g. keep only the last N `instagram_metrics` rows per cut, or
  a periodic cleanup job)? **The design explicitly leaves this undecided** (not in its Open Questions
  section — a gap this report is surfacing as a review trigger, not a blocker: today's `stage_events`
  table already has no retention policy for ANY stage, so this isn't a new category of unbounded
  growth this fix introduces, just a higher steady-state rate for cuts that stay published a long
  time). Recommend: proceed without a retention policy for this fix (consistent with existing
  practice), but note it explicitly as an accepted, pre-existing trade-off in the PR description
  rather than silently inheriting it.
- **No deploy-ordering constraint** — no migration, so no ordering hazard like the reaper-resume
  feature's `claim_token` column. Purely additive, safe to deploy/rollback in any order relative to
  itself (there is no "itself" to order against — one PR, one deploy).
- **No monitoring/alert wiring needed beyond the existing reel-detail page** — confirmed the design's
  claim that `ui/templates/reel.html`'s `stage-fail` styling is generic (not stage-specific CSS) by
  reading `main.css` — applies automatically to the new stage's row.

## Review triggers

1. **Confirm the `missing_metrics` allowlist check is implemented against the raw `data` list's
   `name` keys, not `by_name`'s keys** — this is the exact defect an adversarial design review
   caught before any code existed; a Builder re-introducing it (e.g. by refactoring `by_name`'s
   construction and computing `missing` from it "for simplicity") would silently reopen the bug this
   whole fix exists to close. A dedicated reviewer pass on the merged diff should specifically
   re-verify this, not just trust the design doc's own claim.
2. **Confirm `_pipeline_summary`'s exclusion set is applied to `total_cost`/`total_latency_ms` only,
   never to `stage_summary`** — an over-eager implementation (e.g. filtering `stage_events` itself
   before both computations) would silently make `instagram_metrics` invisible in the per-stage
   table too, undoing the exact visibility this fix is supposed to add.
3. **Confirm the mode-1 (missing-metric) `record_stage` composition never raises** — must follow the
   documented "set `ev.ok = False` inside the `with` block, then return normally" pattern (CLAUDE.md's
   own `record_stage()` composition rule), not an `except`-and-reraise or an `except`-and-swallow-
   without-marking-`ok=False` — both wrong compositions are plausible-looking mistakes a Builder
   could make, and this codebase has a documented history (the YouTube captions-upload feature) of
   exactly this class of composition bug being caught only by review, not by casual testing.

## Unknowns

None material. The design is grounded in the actual current code of every file it touches, and the
adversarial review round already resolved the two real gaps found in the first draft (see the
design doc's own Revision note).

## Evidence refs

- `docs/specs/2026-09-instagram-metrics-drift-system-design.md` (full document, revision note)
- `engine/publish/metrics.py` (full file, current state)
- `worker/tasks/metrics.py` (full file, current state)
- `engine/observability.py:19-59` (`record_stage`)
- `api/routers/reels.py:208-263` (`_pipeline_summary`, `reel_detail`)
- `api/models.py` (`Cut`, `StageEvent` column definitions)
- `ui/templates/reel.html:27-56`, `ui/static/main.css` (`stage-fail` styling, confirmed generic)
- `tests/test_metrics_fetcher.py` (full file), `tests/test_reels_router.py:233-248`,
  `tests/test_metrics_task.py`, `tests/test_publish_registry.py` (all read in full)
