# System design: Instagram Insights metric names may drift — SYSTEM_DESIGN_SPEC

Status: **Ready for implementation planning**
Source item: `docs/roadmap.md` Open Issues table — `Instagram Insights metric names may drift`
(Medium severity)

> **Revision note (adversarial review round 1, before any code existed):** an independent reviewer
> found two real, code-confirmed gaps in the first draft, both now folded into the sections below
> rather than left as open questions: (1) `api/routers/reels.py::_pipeline_summary` sums
> `latency_ms`/`cost_usd` across **every** `StageEvent` for a reel with no stage-name filter, and
> `ui/templates/reel.html` renders that sum under the label **"total LLM time"** — a new stage that
> fires every 6 hours for the entire lifetime a cut stays `published` (months/years) would silently
> inflate that headline stat and grow `stage_events` unboundedly for that reel, which is exactly the
> "don't silently corrupt an existing signal" failure this whole design exists to avoid, just aimed
> at a different existing signal than the one being fixed. Fixed in §3/§7/§8 below by excluding
> post-publish stages from the *aggregate* totals while still surfacing them, individually, in the
> existing per-stage breakdown table (which already has its own count/latency/cost/failures columns
> and needs no change). (2) The originally-planned allowlist check risked comparing against
> `by_name` (the dict already built from the response) rather than the raw response entries —
> `by_name = {d["name"]: d["values"][0]["value"] for d in data if d.get("values")}` silently drops
> any entry whose `values` list is present-but-empty, so a check against `by_name`'s keys would
> inherit that same truthiness bug and miscount an empty-`values` entry as "present," reproducing
> the exact silent-drop failure this design exists to close. Fixed in §3 below: the check compares
> against the raw `data` list's `name` keys, computed *before* `by_name` is built.

---

## 1. Problem and non-goals

**Problem, quoting the roadmap's own description verbatim:** *"`InstagramMetricsFetcher` requests
`plays,likes,comments` — Meta has renamed Reels Insights metrics before (e.g. `plays` →
`video_views` at points), so a future API change could silently return empty/zero metrics rather
than erroring loudly."*

Root cause, confirmed by reading the real code (`engine/publish/metrics.py::InstagramMetricsFetcher`,
`worker/tasks/metrics.py::pull_publish_metrics`/`_pull_one`):

```python
_METRICS = "plays,likes,comments"

def fetch(self, cut, credential, db) -> EngagementMetrics | None:
    resp = httpx.get(..., params={"metric": self._METRICS}, ...)
    resp.raise_for_status()
    data = resp.json().get("data", [])
    if not data:
        return None
    by_name = {d["name"]: d["values"][0]["value"] for d in data if d.get("values")}
    return EngagementMetrics(
        views=by_name.get("plays"),
        likes=by_name.get("likes"),
        comments=by_name.get("comments"),
    )
```

Two distinct drift failure modes, both silent today:

1. **Silent field omission (200 OK, incomplete `data`).** If Meta renames or deprecates a metric,
   the Graph API's insights endpoint has historically just omitted the unrecognized/retired field
   from `data` rather than erroring — the request still returns 200. `by_name.get("plays")` then
   returns `None` forever for that metric. `_pull_one()`'s existing merge logic
   (`if result.views is not None: cut.views = result.views`) was written for the *legitimate*
   "platform has no data yet" case (`fetch()` returning `None` entirely for a just-published
   video) — it cannot distinguish "not yet available" from "permanently renamed," so it silently
   accepts the gap forever. Worse: `cut.metrics_updated_at` is still bumped on every pull
   (`_pull_one`'s unconditional `cut.metrics_updated_at = datetime.now(timezone.utc)` at the end),
   so the cut card's own staleness signal (`"checked every 6h"` hint, gated on
   `metrics_updated_at is None`) reads as healthy even though one or more fields have silently
   stopped updating.
2. **Whole-request failure (metric name removed, not just renamed).** Graph API insights calls are
   all-or-nothing against the requested `metric` parameter — an entirely invalid/retired name in
   the comma-separated list 400s the *whole* request, not just that one field.
   `resp.raise_for_status()` raises, which propagates up to `_pull_one()`'s existing
   `except Exception: _log.exception(...); return` — correctly isolated per-cut (doesn't abort the
   batch), but the only signal is a log line. Since `_METRICS` is a shared class-level constant,
   every Instagram cut hits the identical failure on every 6-hour pull from then on, with nothing
   persisted anywhere an operator would look — only logs, which nobody watches for a background
   task that "usually just works."

**Why this is Medium, not Low:** engagement metrics are the entire input to this codebase's
quality↔engagement correlation feature (`engine/analytics/correlation.py`, `GET /api/insights`) and
the per-cut engagement stats on the cut card. A silent, permanent Instagram data gap doesn't crash
anything — it quietly corrupts an analytics feature that already carries an explicit
statistical-honesty caveat about its own limitations, by silently starving it of an entire
platform's data with no error anywhere.

**Non-goals:**
- Auto-discovering or self-healing a renamed metric (e.g. trying a fallback name list). This design
  makes drift **loud**, not automatically robust — Meta's naming is out of this codebase's control,
  and guessing at replacement names is more likely to silently mask a real problem than solve it.
- Touching `YouTubeMetricsFetcher`. YouTube Data API v3's `statistics` object field names
  (`viewCount`/`likeCount`/`commentCount`) have been stable since the API's introduction; this is
  specifically an Instagram Graph API historical pattern (the roadmap item names Instagram only).
- Changing `worker/tasks/metrics.py`'s per-cut try/except isolation, `_pull_one()`'s partial-merge
  semantics (`if result.X is not None: cut.X = result.X`), or the `EngagementMetrics` dataclass
  contract. All three are correct today and orthogonal to this fix.
- A UI/alerting feature beyond what this codebase's existing conventions already provide (see §9 —
  this reuses an existing, already-rendered surface with zero template changes).

---

## 2. Components

No new components. Two existing pieces gain new internal behavior:

- **`engine/publish/metrics.py::InstagramMetricsFetcher.fetch()`** — the primary file touched for
  logic. Gains: (a) an explicit allowlist check of the requested metric names against what the
  Graph API actually returned (against the raw response, not the derived `by_name` dict — see §3),
  (b) a `record_stage(...)` wrapper around the whole HTTP call + parse, so both failure modes above
  leave a persisted, queryable trail instead of only a log line (mode 2) or nothing at all (mode 1).
- **`api/routers/reels.py::_pipeline_summary()`** — a small, additive change (an exclusion set for
  the two headline aggregate sums; see §8) needed because of the volume/labeling mismatch the new
  stage introduces (§6). No change to its return shape, its callers, or the per-stage
  `stage_summary` table.
- **`engine/observability.py::record_stage()`** — reused as-is, zero changes. This is the existing
  "call succeeded/failed, worth persisting" instrumentation this codebase already applies to every
  external call site with the same shape (LLM calls, HF asset generation, the YouTube captions
  upload best-effort step) — see CLAUDE.md's own `record_stage()` composition rule. A metrics pull
  is exactly this shape: an external HTTP call whose failure must not abort the caller
  (`_pull_one`'s per-cut isolation already guarantees that for mode 2; mode 1 must *also* not abort
  anything, since a partial result is still useful) but whose failure must be visible somewhere
  durable.

---

## 3. APIs / Events / Data model

**No new HTTP endpoints, no schema migration.** The design's only "event" is a `StageEvent` row
(existing table, existing writer function), which the reel's already-existing pipeline
cost/latency/quality panel (`api/routers/reels.py::_pipeline_summary`) already aggregates
indiscriminately by `stage` name with **no stage-name allowlist** — confirmed by reading that
function: it groups every `StageEvent` for a reel by `e.stage` and counts `s["failures"] += 1` when
`not e.ok`, with no special-casing of which stages "count." A new `stage="instagram_metrics"` value
requires zero changes there.

New `StageEvent` field values used (all existing columns):
- `reel_id`: `cut.reel_id` (every `Cut` has this FK; confirmed in `api/models.py`).
- `cut_id`: `cut.id`.
- `stage`: `"instagram_metrics"` (new stage-name string; no enum to update — `stage` is a plain
  `String(50)`; also added to the aggregation exclusion set in §7/§8 below).
- `ok`: `True` for a clean pull (all requested metrics present), `False` for either drift mode.
- `detail`: for mode 1 (silent omission), `{"missing_metrics": [...]}` naming exactly which
  requested metric names were absent. For mode 2 (whole-request failure), `record_stage`'s own
  existing exception-capture path already writes `{"error": repr(exc)}` — no new code needed there,
  just wrapping the call in the context manager at all.

**The "missing" check must compare against the raw `data` list's `name` keys, not against
`by_name`.** `by_name = {d["name"]: d["values"][0]["value"] for d in data if d.get("values")}`
silently drops any entry whose `values` list is present-but-empty (`d.get("values")` is falsy for
`[]`) — an empty-but-present entry is itself a form of drift (Meta returned the field but with no
value where one was expected), and checking against `by_name`'s keys would misclassify it as
"present," reproducing the exact silent-drop bug this design exists to close. Implementation:
compute `returned_names = {d["name"] for d in data}` directly from the raw response, before
building `by_name`, and diff the requested metric names (`self._METRICS.split(",")`) against
`returned_names` — an entry present in `data` with an empty `values` list still counts as
"returned" by name (it's not a naming/renaming drift, `by_name` already handles the empty-value case
correctly by omitting it from the dict, which is the right behavior for that value going through as
`None`); the concern here is specifically about missing **names**, which `returned_names` correctly
captures independent of whether each one's `values` was populated.

**The fully-empty-`data` case is exempt from the *missing-metric flag*, not from the `StageEvent`
write itself — a build-stage correction to this section.** `if not data: return None` (today's
existing "platform has no data yet for a just-published video" path) is unchanged, and the new
allowlist check never runs for it (an empty `data` would otherwise flag every requested metric as
"missing" and spuriously mark every just-published video as drifted on its very first pull — that
part of the original reasoning holds). But the original wording above — "no `StageEvent` write is
added there" — turned out to be incompatible with `record_stage()`'s own contract, which a
Lens B/Contracts-and-Operations review of the built code caught: `record_stage`'s `finally` block
writes and commits the `StageEvent` on **every** normal exit from its `with` block, including an
early `return` executed from inside it — there is no supported way to enter the block (needed
around the HTTP call itself, so a whole-request failure per mode 2 still gets instrumented) and
exit through this one specific branch with zero write, without either (a) moving the HTTP
call itself outside `record_stage`'s instrumentation for this one code path, which would silently
drop mode-2 failure tracking for exactly the class of pull most likely to hit it (a very recently
published video, whose Insights endpoint is also more likely to be in a transient/incomplete
state), or (b) hand-rolling a bespoke non-standard bypass of `record_stage` for one narrow branch,
which this codebase's own conventions advise against (CLAUDE.md's `heartbeat()`/`job_task`
copy-drift note: shared instrumentation helpers exist precisely so call sites don't each
reimplement their own variant).

**Resolution: the empty-`data` case still writes a `StageEvent`, with `ok=True` (the default) and
no `missing_metrics` key** — indistinguishable, by design, from an ordinary clean pull. This is a
deliberate relaxation of the original requirement, not an oversight: `ok=True` carries no
false-failure signal (the entire point of this fix is to make *drift* loud, and "no data yet" is
not drift), and the row's growth is self-limiting in a way `instagram_metrics` rows for a cut that
*does* have data are not — once Instagram populates real Insights data (typically within hours of
publish, not indefinitely), every later pull for that cut has non-empty `data` and this branch is
never hit again for it. §6's unbounded-growth concern is about the steady-state, indefinite,
`published`-status-duration volume from cuts that DO have data; this branch adds at most a handful
of extra rows per cut during a short post-publish window, not a new unbounded category.

---

## 4. State machines / consistency

No state machine changes. `Cut.status` is untouched by this fix (metrics pulls have never gated on
or mutated cut status, and continue not to). Consistency model is unchanged: `_pull_one()`'s
existing partial-merge (`if result.X is not None: cut.X = result.X`) and single `db.commit()` at the
end of a successful pull are untouched — this fix adds a *second*, independent commit (the
`StageEvent` write, via `record_stage`'s own `db.commit()`) that happens **before** `_pull_one`'s
existing commit, inside `fetch()`, on a different logical entity (an audit row, not a `Cut`
mutation). `record_stage`'s docstring already documents this as its calling convention ("every
current call site sits on a commit boundary"); this design does not introduce a new pattern, it
uses the existing one.

**One subtlety to get right on the mode-1 (silent omission) path:** the `StageEvent` write must
happen *unconditionally whenever any requested metric is missing*, but must **not** raise — a
partial result (2 of 3 metrics present) is still useful data that `_pull_one()` should apply via its
existing per-field merge, exactly like the "platform has no data yet for `comments`" case it already
handles today. This is the same "must never fail the caller, but must still tell the truth" shape
CLAUDE.md's `record_stage()` composition rule already documents for the YouTube captions-upload
best-effort step: put the `try`/`except`-equivalent check **inside** the `with record_stage(...) as
ev:` block, explicitly set `ev.ok = False` and `ev.detail[...]` on detection, and let the function
return its (partial) `EngagementMetrics` normally — never let the missing-metric case itself raise.
A genuine HTTP failure (mode 2, `resp.raise_for_status()`) is the one case that *should* propagate
through `record_stage`'s own re-raise, exactly as it does for every other `record_stage` call site
in this codebase — `_pull_one()`'s existing `except Exception` still catches it unchanged.

---

## 5. Retries / idempotency

Unchanged. `pull_publish_metrics()` already runs unconditionally every 6 hours (Celery beat) and
each call is independently idempotent (it's a read + overwrite of the metrics columns, not an
append). This fix adds no new retry logic — a metric that's actually renamed will simply keep
producing `ok=False` `StageEvent` rows every 6 hours until an operator updates `_METRICS` to match
Meta's current field names, which is the intended, honest behavior (loud and repeated, not
"retried" in the sense of backoff/attempts).

---

## 6. Capacity

One additional `StageEvent` row per Instagram cut per 6-hour pull. Unlike every other stage
currently written into this table (`generate`/`judge`/`visuals`/`composite`/`asset_hf_*`/
`captions_upload`), each of which fires a small, bounded number of times across a reel's
generation-and-render lifecycle, `instagram_metrics` fires **every 6 hours for as long as the cut
stays `published`** — unbounded, potentially months or years of rows per published Instagram cut.
No indexing changes needed (the existing `StageEvent.reel_id`-filtered query shape is unaffected),
but this volume difference is exactly why the aggregation change in §7/§8 below is required, not
optional: without it, this new stage's own accumulating latency would silently inflate a headline
stat this design's whole premise is to stop silently corrupting.

---

## 7. Failure strategy

| Scenario | Before this fix | After this fix |
|---|---|---|
| Metric silently renamed/omitted (200 OK, incomplete `data`) | Silent forever; `cut.metrics_updated_at` advances as if healthy | `StageEvent(stage="instagram_metrics", ok=False, detail={"missing_metrics": [...]})` written every pull; reel's pipeline panel shows a nonzero, growing "failures" count for that stage; partial data (whatever metrics *were* returned) still applied via existing merge logic — no regression on the happy path |
| Metric name entirely invalid (whole request 400s) | Logged only (`_log.exception`), silently repeats every 6h forever | Same log line (unchanged — `_pull_one`'s per-cut catch is untouched) **plus** a persisted `ok=False` `StageEvent` with the real exception in `detail["error"]`, via `record_stage`'s existing exception path, visible on the reel's pipeline panel exactly like any other failed pipeline stage |
| Transient network blip / real 5xx from Graph API | Logged, retried next 6h cycle | Unchanged behavior, now also gets an `ok=False` `StageEvent` — a single transient blip looks identical to real drift in this table until it self-resolves next cycle; this is an accepted, honest trade-off (see §10) rather than a new problem this fix introduces, since the alternative (suppressing transient-looking failures) would re-introduce exactly the "silently swallow real drift" risk this fix exists to close |

---

## 8. Observability

This design's entire fix **is** an observability fix: it takes a failure mode with zero persisted
signal (mode 1) or a log-only signal (mode 2) and gives both a `StageEvent` row on the affected
reel(s), surfaced through a table that already exists. Two distinct surfaces in that existing
template, handled differently:

- **The per-stage breakdown table** (`ui/templates/reel.html`'s `stage-table`, looping
  `stage_summary.items()`) needs **zero changes**: `_pipeline_summary` already groups by `e.stage`
  with no allowlist, so `"instagram_metrics"` gets its own row automatically, with its own
  `count`/`latency_ms`/`cost_usd`/`failures` — including the already-existing, already-styled
  `{% if s.failures %}` → `.stage-fail` highlight (confirmed present in both files) for a nonzero
  failure count. This is the intended, correct visibility surface for this fix and needs no design
  changes.
- **The three headline `pipeline-stats` boxes** (`total_cost`, `total_latency_ms` — labeled **"total
  LLM time"** in the template — and `quality_score`) are a *different* aggregate, computed in
  `_pipeline_summary` by summing `latency_ms`/`cost_usd` across **every** `StageEvent` for the reel
  with no stage filter. Because `instagram_metrics` fires indefinitely (§6) while every other stage
  fires a bounded number of times, including it in these two sums would make "total LLM time"
  silently drift upward forever on any reel with a long-lived published Instagram cut, and would be
  factually wrong regardless of volume (an HTTP metrics-pull round-trip is not LLM time). **Fix**:
  `_pipeline_summary` excludes a small, explicit set of non-generation/render stage names —
  initially just `{"instagram_metrics"}`, named as a module-level constant (e.g.
  `_EXCLUDED_FROM_TOTALS`) rather than inlined, so a future post-publish stage (e.g. a hypothetical
  future YouTube metrics instrumentation) is an obvious one-line addition, not a second silent
  gap — from the `total_cost`/`total_latency_ms` sums only. `stage_summary` (the per-stage table)
  is built from the same `stage_events` list **unfiltered** — every stage, including excluded ones,
  still gets its own row there; only the two headline sums skip excluded stages. `quality_score` is
  unaffected either way (it's derived from `Job.meta`, not `StageEvent`, and was never at risk).

No new dashboard, no new alerting channel, no new StageEvent-reading code path beyond the one
filtering tweak above — this remains the same single, existing aggregator
(`api/routers/reels.py::_pipeline_summary`) this codebase already relies on, consistent with
`engine/observability.py::latest_quality_scores()`'s own module-layout comment about deliberately
avoiding a second near-identical copy of shared aggregation logic.

---

## 9. Rollout plan

Single-PR, no migration, no config change, no feature flag needed — this is a pure internal-behavior
change to one method plus one new (backward-compatible) `stage` string value. Rollout is: merge, next
scheduled `pull_publish_metrics()` beat tick (≤6h) starts writing the new `StageEvent` rows.
Safe to roll back by reverting the commit — no data written by this fix is required by anything else,
and no existing data is migrated or altered.

---

## 10. Open questions (explicit, not silently folded into Ready)

- **Distinguishing "genuinely renamed" from "one transient blip that happened to omit a field
  this one time"** is not attempted — a single 6-hour cycle with a missing metric writes exactly
  one `ok=False` `StageEvent`, identical in shape to real, permanent drift. An operator watching the
  reel panel would need to notice a *pattern* (repeated failures across multiple pulls) to
  distinguish transient from permanent. This design does not add a "N consecutive failures"
  escalation threshold — that would add real complexity (a place to persist a consecutive-failure
  counter across pulls, a decision about what "escalated" means/does) for a Medium-severity item
  whose whole ask was "surface drift instead of silence," not "auto-triage drift." If this proves
  noisy in practice, a follow-up could bucket by looking at recent `StageEvent` history for the
  stage before deciding whether to also do something more emphatic — deliberately left to a
  follow-up, not designed here.
- **Whether `_METRICS`'s allowlist should live as a class attribute (current) or a `Settings` field**
  (so an operator could update it without a code deploy when Meta renames a metric) is left
  unchanged — out of scope; the fix is about *detecting* drift loudly, not about making recovery
  from drift config-driven. Today's fix path for a real rename is still "an engineer edits
  `_METRICS` and redeploys," same as before this design.

---

## Readiness verdict: **Ready for implementation planning.**

Grounded in the actual current code of all three files named in the roadmap item and the task
brief (`engine/publish/metrics.py`, `worker/tasks/metrics.py`, `engine/observability.py`), reuses
existing conventions with no new abstractions, touches exactly one method's implementation, and
requires no schema/API/template changes. Scope is deliberately narrow per the task brief.
