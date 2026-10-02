# beat_enrichment.py niche branching — module design (Phase 7u)

## Problem

`engine/generation/beat_enrichment.py` had no `niche` parameter anywhere in its
interface — `_is_shallow_beat()`'s tactical-marker gate and `_enrich_batch()`'s system
prompt were both hardcoded football ("You are a football tactical analyst."). This
module is invoked from **both** generation paths: the structured path (legitimately
football-locked — `script_parser.py`'s own docstring confirms the GOALKEEPER/DEFENSE/
MIDFIELD section-label format is football-shaped by design, not incidentally) and the
standard LLM path's `_enrich_standard_path_guide()`, which `prompt.py`/`build_messages()`
explicitly treat as niche-generic (its own worked example niche is "personal finance").

Surfaced as the Top recommendation ("Strong") of a full-codebase
`improve-codebase-architecture` review — the same class of niche-blindness bug
`evaluator.py` already fixed for its own scoring axes (Phase 6c), on a sibling module
that was never brought along.

Confirmed via direct code read, not assumed: `_is_shallow_beat()` gates on
`bool(stub.player)` (a name extracted via `visual_fallback._first_person()`), so for a
non-football niche whose beats rarely name a specific person, `_enrich_with_insight()`'s
`shallow` list is often empty and the function returns before ever calling the LLM — the
bug only actually fires when non-football content *does* name someone (e.g. a finance
reel naming "Warren Buffett"), narrower than "every non-football reel" but still real
and untested.

## Decision (settled via `/grilling`)

Thread `niche: str` into `_is_shallow_beat()`, `_enrich_batch()`, and
`_enrich_with_insight()` symmetrically — structured path passes `reel.niche or ""`
(the column is nullable, unlike `MasterGuide.niche` which Pydantic requires non-null),
standard path passes `guide.niche` directly. Both call sites already had the value at
hand; no new plumbing needed.

The non-football tactical-marker vocabulary reuses `evaluator.py`'s existing
`_INSIGHT_TACTICAL_UNIVERSAL` regex (same conceptual gate — "does this VO already
contain substantive reasoning" — rather than inventing a 5th independently-drifting
vocabulary table, echoing a sibling finding from the same review round about 4 duplicated
person-name regexes). No import cycle exists between the two modules in either direction.

The system prompt and its BAD/GOOD worked example both branch on niche: football keeps
its existing wording verbatim; every other niche gets `"You are a content analyst for a
{niche} video."` and a generic finance-flavored worked example — `Reel.niche` is
operator-entered free text (not untrusted user content), so interpolating it directly is
fine.

Deliberately out of scope: `_make_conflict_stub()`'s "You are writing voiceover for a
sports video" framing is left untouched — it's only ever called from the structured
path (football-locked by design), never reached by non-football content today, so fixing
it would expand the diff with no behavioral payoff. `_is_shallow_beat()`'s
`bool(stub.player)` requirement is also left untouched — loosening it to gate non-person
beats into enrichment for non-football niches is a separate, bigger design question the
architecture review didn't raise.

## Test strategy

5 new tests in `tests/test_enrichment.py`:
- `test_enrich_batch_football_niche_uses_the_football_system_prompt` /
  `..._non_football_niche_uses_a_generic_system_prompt` — confirm the right system
  prompt is selected.
- `test_is_shallow_beat_football_niche_uses_the_football_tactical_regex` /
  `..._non_football_niche_uses_the_universal_tactical_regex` /
  `..._non_football_niche_without_universal_markers_is_shallow` — confirm the regex
  branch is genuinely exercised (not just coincidentally matching both ways), and that
  the "neither vocabulary matches" path is distinct from the "too short" path
  `_is_shallow_beat()`'s `len() < 25` OR'd condition already covers.

1 new test in `tests/test_generate_task.py` — `_enrich_standard_path_guide()` had zero
direct tests before this (every existing test patches it out with no assertion on call
args), so the niche-threading wiring itself — as opposed to the pure function's own
branching logic — was untested. `test_enrich_standard_path_guide_threads_the_guides_real_
niche_through()` closes that: calls the real function with a real `MasterGuide`, mocks
only `_enrich_with_insight`, and asserts the niche argument it receives matches
`guide.niche`.

834 default-run tests pass (828 existing + 6 new; 835 total including the deselected
golden test).

Mutation-tested: (1) hardcoded the vocabulary selection in `_is_shallow_beat()` to
always use the football branch — the non-football universal-vocabulary test failed
correctly. (2) hardcoded the wiring call site in `_enrich_standard_path_guide()` to pass
`"football"` instead of `guide.niche` — the new wiring test failed correctly. Both
restored and re-verified.

## Corrections

None yet — this section will be updated after the 4-persona review round on the opened
PR, per this pipeline's standard practice.
