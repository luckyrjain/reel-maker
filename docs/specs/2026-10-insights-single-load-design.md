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

## Tests (23 new in test_correlation.py, 23 new in test_insights_router.py, 173 in the new test_display.py)

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

## Found by the round-2 review, and fixed in this PR

- **Memory held through the render.** The shared `stats` (every reel, with every cut's guide JSON)
  stayed referenced in the handler until the response was built -- about 170 MB at 2000 reels x 3
  cuts x 25 beats, where `main` freed it between the two analyses. The peak did not change (it
  happens during the load), but the memory outlived its use. The page's context is now built in
  `_insights_context()`, which returns before the template renders, so the reels are freed first.
  Pinned by a test that holds a weakref to every `Reel` and asserts all are collected by the time
  `TemplateResponse` runs.
- **Control and bidi-override characters reached the page.** Jinja autoescape handles HTML only.
  NUL and other C0/C1 controls, and the bidi overrides/embeddings/isolates (U+202A-202E,
  U+2066-2069) in `niche`, hook text or a performance note, were rendered as-is: an override can
  reorder the text around it so that it reads differently from what is stored. This predates the
  PR (same template, same path); it is fixed here at the user's request. `api/display.py::safe_text`
  removes them at display time (whitespace controls become one space); the stored value is never
  rewritten. The directional MARKS (LRM, RLM, ALM) are kept deliberately: ordinary Arabic/Hebrew
  uses them. Applied as a Jinja filter to the three free-text fields on the insights page and in the
  notes fragment (which the create/toggle routes also return). Other pages that show `niche` or hook
  text (reel list, cut card) are not changed here.
- **Tests that did not exist for behaviour this change owns:** a reel with 0 views or quality 0 is
  data, not "missing" (four truthiness mutants had survived); performers are ranked by views, not
  quality; the sign of `r`; the page's top/bottom tables list the right reel ids in the right order
  and the combined table below `2k` reels; `_hook_vo_for_max_views_cut` degrades to `None` for a
  missing or malformed guide, a non-hook first beat and a blank hook line.

## Found by the round-3 review, and fixed in this PR

- **A lone surrogate in a hook line returned a 500.** `Cut.guide` is a JSON column and JSON text can
  carry the `\ud800` escape; the response then cannot be encoded as UTF-8. (`niche` cannot hold one:
  the insert fails.) `safe_text` now removes U+D800-DFFF. A real astral character such as an emoji
  is a different code point and is kept.
- **A value with nothing visible rendered a blank cell.** `"\n"` became `" "`, which is truthy, so the
  template's `or "—"` never ran. `safe_text` now trims outer whitespace, so such a value is empty and
  the fallback shows. The filter must stay BEFORE the `or` (a test pins that). The `\x1c`-`\x1f`
  information separators are now whitespace (one space), like Python's `str.isspace()` / `str.split()`
  (`str.splitlines()` does not split on `\x1f`, so it is not the right comparison), instead of being
  deleted.
- **Tests for the new code:** an interior NBSP, soft hyphen, ZWNJ (Persian), ZWSP, word joiner and
  BOM are kept (outer Unicode spaces, NBSP included, are trimmed by `.strip()`); every whitespace
  run collapses (not just the first); falsy non-None values (0, False) are shown; HTML in the
  niche cell and in a note is escaped; both fallbacks show; notes are listed once each, newest
  first.

## Test gaps found by the round-4 and round-5 reviews, and closed in this PR

- **Round 4:** the trim must run AFTER the removals (`"\x00 x"` is `" x"` until
  the control is gone); outer Unicode spaces (U+3000, U+2003, NBSP, U+202F, U+1680) are trimmed but
  kept inside text; and the code points just outside every stripped or collapsed range are kept
  (U+D7FF/U+E000 around the surrogates, U+10000, U+2027, U+202F, U+2065, U+206A, U+FFFD), so a range
  that is one code point too wide fails a test.
- **Round 5:** the page's own output was barely asserted, so a template regression
  could pass every test: the correlation line (r to two decimals with its sign, the real n), the
  thousands separator, the Quality column, each row's link to its own reel (ids that differ from the
  list position), the same-score branch and all three empty-state messages, and the notes markup
  (toggle and delete URLs use the note id; `checked` and `line-through` on the right rows; notes
  ordered by creation time, not id). Also the trim is checked against EVERY character Python calls
  whitespace, not six samples. Ordering of reels with equal views is unspecified (no ORDER BY on
  reels), and a code decision such as a `reel_id` tie-break would be needed to pin it: left as is.

## Left alone

- **Hidden Unicode TAG characters (U+E0000-E007F) in a performance note.** They are invisible in the
  browser but `generate.py` puts active notes into `prior_feedback`, so the LLM receives them: a
  hidden-instruction ("ASCII smuggling") route for text an operator pastes in. Stripping them in
  `safe_text` would not change what the LLM gets; it has to happen when the note is written
  (`create_note`) or when notes are seeded. Not changed here: the tag characters also spell the
  England/Scotland/Wales flag emoji, so stripping them degrades those flags to a plain black flag.
  Needs an owner's decision. Implemented separately in PR #47 (which also covers a reel's context
  and niche).
- Other invisible or layout characters (ZWSP/ZWNJ/ZWJ, word joiner, BOM, soft hyphen, variation
  selectors, odd spaces) pass through on purpose: on this page they are cosmetic, and ZWJ/ZWNJ are
  part of correct emoji, Indic and Persian text.
- Other pages that show `niche` or hook text (reel list, reel page, cut card) are not changed here.
- `stats` is an optional parameter, so a future caller can forget it and silently pay for a second
  load. It costs time, not correctness, and the page-level query-count test guards the only caller.
- The load still reads every cut's `guide` JSON to serve at most six hook lines. Deferring
  `Cut.guide` and reading it only for the shown performers would cut the page's memory, but changes
  loading behaviour; a separate change.
- The jobs query loads every job type. `latest_quality_scores()` only reads generate jobs, so a
  `Job.type == generate` filter would be a cheap follow-up (the original note in the code).
- `api/routers/reels.py::_reel_list_metrics()` computes a similar reel -> (quality, views) pairing for
  the reel list page. It is a different page with its own query shape; not merged here.
- `_hook_vo_for_max_views_cut()`'s `if not guide.beats` check is unreachable: `PlatformGuide`
  rejects an empty `beats` list at validation, which the `except` above already turns into `None`.
  Left as is (pre-existing, harmless).
