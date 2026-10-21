# Insights page: one load, two analyses (Phase 8b, candidate 5)

## Problem

`GET /api/insights` called `quality_engagement_correlation(db)` and `top_bottom_performers(db)`.
Each began with the same work: read every `Reel` with its `Cut`s (`joinedload`, so every cut's
`guide` JSON), read every `Job`, derive each reel's latest quality score and max-views figure, and
keep reels that have both. So the page ran those queries twice and kept two copies of the
"which reels count" rule, which had to be changed in step.

## Change

- `load_reel_stats(db) -> list[ReelStat]`: the one read and the one rule. `ReelStat` holds the
  reel, its latest quality score and its max-views figure; only reels with both are included.
- `quality_engagement_correlation(db, stats=None)` and `top_bottom_performers(db, k=3, stats=None)`
  take that list. The router loads it once and passes it to both.
- `stats=None` still loads its own, so every existing caller and all 21 pre-existing tests are
  unchanged. `stats=[]` means "no qualifying reels", not "load them": the check is `is None`.

A pure refactor: every pre-existing `test_correlation.py` and `test_insights_router.py` test passes
against it with its body and assertions unmodified (checked before any test was added). The only
edits to existing test code are the new imports and one fixture line (`c._engine = engine`) that the
new page-level test needs.

## Tests (13 new in test_correlation.py, 1 new in test_insights_router.py)

- `load_reel_stats` keeps only reels with both quality and views, and takes the max-views cut.
- Both analyses given shared stats equal their self-loading results.
- Given stats, neither issues any SELECT.
- `stats=[]` is respected and not reloaded.
- The page issues exactly one SELECT on `reels` and one on `jobs`, and none on a standalone `cuts`
  (SQLAlchemy `before_cursor_execute`). Cuts arrive through the reels `joinedload`, so a lazy per-reel
  load or an extra cuts query would show up as `FROM cuts` and re-read every guide.

A review round added tests for behaviour that no test pinned before this change but that the shared
load now owns: `k` sets the size of both lists, the latest generate job's score wins by `created_at`
(not insertion order), and the hook comes from the max-views cut at any position of `reel.cuts`.
That last one is parametrized over all 6 orderings, because SQLite returns `reel.cuts` in an arbitrary
order (a two-cut fixture happened to put the maximum first, and a mutant picking the first cut
survived it).

Mutation-checked, each failing for the predicted reason: router back to the old double call (either
analysis), `if not stats` in either analysis, an analysis ignoring the passed stats, dropping the
quality filter.

## Left alone

- `stats` is an optional parameter, so a future caller can forget it and silently pay for a second
  load. It costs time, not correctness, and the page-level query-count test guards the only caller.
- The load still reads every cut's `guide` JSON to serve at most six hook lines. Deferring
  `Cut.guide` and reading it only for the shown performers would cut the page's memory, but changes
  loading behaviour; a separate change.
- The jobs query loads every job type. `latest_quality_scores()` only reads generate jobs, so a
  `Job.type == generate` filter would be a cheap follow-up (the original note in the code).
- `api/routers/reels.py::_reel_list_metrics()` computes a similar reel -> (quality, views) pairing for
  the reel list page. It is a different page with its own query shape; not merged here.
