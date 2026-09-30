# System design: `pull_publish_metrics()` overwrites rather than accumulates a time series

**Status:** Proposed, revised once after adversarial review (see §10). **Severity:** Low per
`docs/roadmap.md`'s Open Issues table.

## 1. Problem

`docs/roadmap.md`'s Open Issues table: *"`pull_publish_metrics()` overwrites rather than
accumulates a time series — `cuts.views`/`likes`/`comments` hold only the latest pull — see Phase
5b for the 'sortable table, not a dashboard' reasoning. A trend line needs a separate
metrics-history table later."*

`worker/tasks/metrics.py::_pull_one()` (called every 6h by the `pull_publish_metrics` Celery beat
task) fetches each published cut's current views/likes/comments and overwrites
`Cut.views`/`Cut.likes`/`Cut.comments`/`Cut.metrics_updated_at` in place. Every prior pull's values
are discarded — there is no way to see how a cut's engagement changed over time, only its current
state.

## 2. Scope

This closes the literal wording of the Open Issues row: **accumulate** a history instead of only
overwriting the latest snapshot. It does **not** add a trend-chart UI, engagement-rate
normalization, or any new analytics endpoint — those are separate, larger gaps already listed
independently in `docs/roadmap.md`'s "What is not yet wired in" section ("Analytics beyond raw
views/likes/comments... no trend charts... no engagement-rate normalization... no per-axis
correlation"). Building a chart now would be solving a problem this specific roadmap row doesn't
ask for; the row's own wording is about the *data*, not its presentation.

## 3. Design decisions

**A new table, `cut_metric_snapshots`, written alongside the existing `Cut.views`/`likes`/`comments`
columns — never replacing them.** `Cut.views`/`likes`/`comments`/`metrics_updated_at` stay exactly
as they are: the "latest known snapshot" columns every existing reader depends on
(`engine/analytics/correlation.py`, `api/routers/reels.py`'s reel-list view, `cut_card.html`,
`insights.html`, `reels_list.html`). None of those readers change. The new table is purely additive
— a second write, not a schema migration of the existing columns.

**Modeled on `StageEvent`'s own shape, this codebase's existing precedent for an append-only
instrumentation log** (`api/models.py::StageEvent`, `stage_events` table): a plain model with an FK
to its parent, no ORM relationship/back_populates declared (queried directly, the same way
`StageEvent` rows are queried via `record_stage()`'s callers rather than through a `Reel.stage_events`
collection), and a `created_at`-style timestamp column with a Python-side default. No cascading
delete concern to design for, since neither `Cut` nor `Reel` rows are ever deleted in this
application (no delete endpoint exists for either).

```python
class CutMetricSnapshot(Base):
    """One row per pull_publish_metrics() reading for a cut -- an append-only history
    alongside Cut.views/likes/comments' "latest known" columns (unchanged, still the
    single source every existing reader uses). Closes the "pull_publish_metrics()
    overwrites rather than accumulates" Open Issues item; no consumer for this table
    exists yet -- see docs/specs/2026-09-metrics-history-system-design.md."""
    __tablename__ = "cut_metric_snapshots"

    id = Column(Integer, primary_key=True)
    cut_id = Column(Integer, ForeignKey("cuts.id"), nullable=False)
    views = Column(Integer)
    likes = Column(Integer)
    comments = Column(Integer)
    recorded_at = Column(DateTime(timezone=True), default=_now, server_default=func.now(), nullable=False)
```

`server_default=func.now()` mirrors both the Python-side `default` (see §10, Correction 1) — the
same paired `default`/`server_default` pattern this codebase already uses for
`Job.reaper_resumes`/`Job.claim_token` (migration `0013`), needed because `tests/conftest.py` builds
its schema via `models.Base.metadata.create_all(engine)` (not alembic), so the MODEL's own column
definition — not the migration — is what actually governs the schema in every test in this suite.

An index on `(cut_id, recorded_at)` is added in the migration (cheap now, avoids a second
migration later for the query pattern any future trend-chart consumer will need: "every snapshot
for cut X, ordered by time").

**Record the fetch's raw, possibly-partial values — not the post-merge `Cut` columns.** `_pull_one()`
already merges a partial `EngagementMetrics` result field-by-field (`if result.views is not None: cut.
views = result.views`), so a metric genuinely absent from one pull (e.g. the Instagram metric-drift
scenario `engine/publish/metrics.py`'s `InstagramMetricsFetcher` already detects) leaves the
*existing* `Cut.views` value in place rather than nulling it — correct behavior for the "latest known"
columns, which should never regress to unknown once a real value has been seen. A history row must
NOT inherit that same forward-fill: recording the carried-forward `cut.views` value under a *new*
timestamp would fabricate a data point that was never actually measured at that time, corrupting any
future trend line with a false flat segment. The snapshot row stores `result.views`/`result.likes`/
`result.comments` directly — each individually `None` when that specific pull didn't return it,
exactly mirroring `EngagementMetrics`' own nullable-field contract.

**One `now` captured once per pull, used for both the snapshot's `recorded_at` and the existing
`Cut.metrics_updated_at`.** `_pull_one()` currently calls `datetime.now(timezone.utc)` once, inline,
when setting `cut.metrics_updated_at`. This design captures that value into a local variable first
and reuses it for the new snapshot's `recorded_at`, so the two timestamps for the same pull are
byte-identical rather than differing by whatever microseconds elapse between two separate `now()`
calls — a small correctness/testability improvement (a test can assert exact equality between
`cut.metrics_updated_at` and the snapshot's `recorded_at`, not just "close in time"), not a
behavior change either column-consumer depends on.

**A snapshot is written whenever a real fetch happens — same gate as the existing `Cut` column
update, not a stricter one.** `_pull_one()`'s existing early returns (no fetcher for the platform, no
connected credential, a fetch exception, `result is None`) already mean "nothing was learned this
pull" — none of those paths currently touch `Cut.metrics_updated_at` either, and the new snapshot
row follows the identical gate: written in the same branch, right where `cut.metrics_updated_at` is
already set, never before it and never on a path that doesn't already update the existing columns.
This means even a pull where `EngagementMetrics(views=None, likes=None, comments=None)` comes back
(the pathological all-fields-missing case) still writes a snapshot row — recording "we tried and
got nothing" is itself a meaningful data point for a future gap-detection use of this table, distinct
from "we never tried" (no row at all).

**No pruning/retention policy.** At the existing 6-hour beat cadence, one snapshot row per published
cut every 6 hours is roughly 1,460 rows per cut per year — trivially small for any SQLite/Postgres
deployment this single-operator tool targets. Building retention logic now would be solving a
problem this scale doesn't have, matching this codebase's own stated aversion to speculative
infrastructure (see `docs/roadmap.md`'s Pixabay/per-axis-correlation entries for the same reasoning
applied elsewhere).

## 4. Data model / migration

New migration `0015_cut_metric_snapshots.py`:

```python
def upgrade() -> None:
    op.create_table(
        "cut_metric_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cut_id", sa.Integer(), sa.ForeignKey("cuts.id"), nullable=False),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.Column("likes", sa.Integer(), nullable=True),
        sa.Column("comments", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_cut_metric_snapshots_cut_id_recorded_at",
        "cut_metric_snapshots", ["cut_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_cut_metric_snapshots_cut_id_recorded_at", table_name="cut_metric_snapshots")
    op.drop_table("cut_metric_snapshots")
```

`api/models.py` gains `CutMetricSnapshot` (§3). `docs/data-model.md` gains a new table entry.

## 5. Implementation

`worker/tasks/metrics.py::_pull_one()`, after the existing per-field merge:

```python
    now = datetime.now(timezone.utc)
    if result.views is not None:
        cut.views = result.views
    if result.likes is not None:
        cut.likes = result.likes
    if result.comments is not None:
        cut.comments = result.comments
    cut.metrics_updated_at = now
    db.add(models.CutMetricSnapshot(
        cut_id=cut.id, views=result.views, likes=result.likes, comments=result.comments,
        recorded_at=now,
    ))
    db.commit()
```

## 6. Failure strategy

Unchanged from today — this design adds no new failure mode. A fetcher exception is still caught
and logged by the existing per-cut `try/except` in `_pull_one()`'s caller loop, before any of the
new code runs; the snapshot write shares the exact same commit as the existing column update, so a
DB error there fails identically to how an existing-column write failure already would (uncaught,
propagating to `pull_publish_metrics()`'s per-cut isolation — no new fragility introduced).

## 7. Observability

No new `StageEvent` — this isn't an LLM/paid call or an external API call in its own right (the
fetch itself is already whatever `MetricsFetcher.fetch()` instruments, e.g.
`InstagramMetricsFetcher`'s existing `record_stage(..., "instagram_metrics", ...)` call). Writing
one extra DB row alongside an already-committing write needs no separate instrumentation.

## 8. Rollout plan

Additive migration (new table, no existing column touched) — safe to run ahead of a code deploy or
after, in either order, since old code never references the new table and new code degrades to "no
history yet" for any cut whose migration hasn't run (impossible in practice, since the migration and
code ship together, but stated for completeness). No backfill: history starts accumulating from the
first `pull_publish_metrics()` run after this ships. Existing `Cut.views`/`likes`/`comments` values
are untouched, so the "latest snapshot" experience (cut card, reel list, insights page, correlation)
is unaffected on day one.

## 9. Test plan

`tests/test_metrics_task.py` (extends the file's existing real-`db_session` pattern — no mocking of
the DB layer, so a new table's rows can be queried directly):

1. A successful full-metrics fetch writes both the existing `Cut` column update AND a matching
   `CutMetricSnapshot` row with the same `views`/`likes`/`comments`, and `recorded_at` exactly equal
   to `cut.metrics_updated_at` (proving the single-`now()`-capture design, not just "close in time").
2. A partial fetch (e.g. `EngagementMetrics(views=100, likes=None, comments=None)`, the Instagram-
   drift-style case) writes a snapshot with `likes=None`/`comments=None` even when `Cut.likes`/
   `Cut.comments` retain their prior non-None values from an earlier pull — proving the "record raw
   result, not the forward-filled cut columns" design decision empirically, the property most likely
   to regress if a future edit "simplifies" this to `views=cut.views` etc.
3. `result is None` (no data yet) writes no snapshot row and leaves `Cut.views` untouched — the
   existing `test_none_result_leaves_existing_metrics_untouched` test extended with a row-count
   assertion.
4. A fetcher exception (existing `test_one_cuts_fetch_failure_does_not_abort_the_batch` scenario)
   writes no snapshot row for the failing cut, one for the succeeding cut in the same batch.
5. No credential / no fetcher for the platform (existing tests) writes no snapshot row.
6. **(Added per §10, Correction 2)** A fully-`None` result (`EngagementMetrics(views=None,
   likes=None, comments=None)` — the platform responded but every metric was unavailable, not "no
   data at all yet") still writes a snapshot row with all three fields `None`, per §3's explicit
   "we tried and got nothing is itself a meaningful data point" design decision. This is exactly the
   behavior most likely to get quietly "simplified away" by an early-return guard during
   implementation (e.g. `if not any([result.views, result.likes, result.comments]): return` would
   look like a harmless optimization but would silently defeat this specific design choice) — a
   dedicated test pins it down rather than leaving it implicit in test 2's partial case alone.

Every new assertion is written to fail against a version of `_pull_one()` with the snapshot-write
line removed (test 1) or with `views=cut.views` substituted for `views=result.views` (test 2, the
forward-fill regression) before being confirmed to pass against the real fix — mutation-tested per
this pipeline's standing convention.

## 10. Corrections (adversarial review, before any code was written)

An independent review of this design doc against the actual code (`worker/tasks/metrics.py`,
`engine/publish/metrics.py`, `api/models.py`'s `Cut`/`StageEvent`, migrations `0002`/`0007`/`0013`/
`0014`, `tests/test_metrics_task.py`, `tests/conftest.py`) confirmed every factual claim about
`_pull_one()`'s current behavior, `EngagementMetrics`' nullable-field contract (and that
`InstagramMetricsFetcher`'s partial-fetch scenario is a real, already-shipped code path, not
hypothetical), the `StageEvent` precedent (no relationship anywhere references it), the migration
syntax matching house style, and the "Cut/Reel are never deleted" claim (grepped every `.delete()`
call site in the codebase — none touches Cut or Reel). It found two real gaps, both closed above:

1. **Missing `server_default` on the model column.** The original draft put `server_default=sa.func.
   now()` only in the migration sketch, not mirrored on the `CutMetricSnapshot.recorded_at` model
   column. This breaks the repo's own established pattern for exactly this situation —
   `Job.reaper_resumes`/`Job.claim_token` declare both `default=...` and `server_default=...` on the
   model, matching their migration. Two concrete consequences the review traced: a future `alembic
   revision --autogenerate` (this repo's own documented workflow) diffs against the model, not
   migration history, and would likely propose dropping the "phantom" server default; and
   `tests/conftest.py` builds its schema via `models.Base.metadata.create_all(engine)`, not alembic
   — so the model's own column definition is what actually governs the schema in every test in this
   suite, making the migration's server default dead code from the test suite's perspective. Fixed
   by adding `server_default=func.now()` to the model column too.
2. **Test plan gap: the fully-`None`-result case.** §3's design explicitly calls out this behavior
   as intentional and non-obvious, but none of the originally-planned 5 tests exercised it. Added as
   test 6 above.

No other factual claims in the design were found to be wrong; the single-`now()`-reuse design,
cascading-delete reasoning, and migration-syntax claims all held up against direct code inspection.
