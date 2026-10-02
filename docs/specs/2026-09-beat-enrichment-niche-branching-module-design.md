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

Thread `niche` into `_is_shallow_beat()`, `_enrich_batch()`, and
`_enrich_with_insight()` symmetrically — structured path passes `reel.niche`
(the column is nullable, unlike `MasterGuide.niche` which Pydantic requires non-null),
standard path passes `guide.niche` directly. Both call sites already had the value at
hand; no new plumbing needed. (The first version of this fix passed `reel.niche or ""`
and used an exact-set-membership football check — see §Corrections for why that was
replaced.)

The non-football tactical-marker vocabulary reuses `evaluator.py`'s existing
`_INSIGHT_TACTICAL_UNIVERSAL` regex (same conceptual gate — "does this VO already
contain substantive reasoning" — rather than inventing another independently-drifting
vocabulary table; a separate finding from the same review round noted duplicated
person-name regexes elsewhere). No import cycle exists between the two modules in either direction.

The system prompt and its BAD/GOOD worked example both branch on niche: football keeps
its existing wording verbatim; every other niche gets `"You are a content analyst for a
{niche} video."` and a generic finance-flavored worked example. The niche is operator-entered free text on the
structured path and LLM-generated on the standard path, so it passes through
`_clean_niche()` (control characters/newlines stripped, whitespace collapsed, capped at 64)
before being interpolated into the system prompt — added after review, see §Corrections.

Deliberately out of scope: `_make_conflict_stub()`'s "You are writing voiceover for a
sports video" framing is left untouched — it's only ever called from the structured
path (football-locked by design), never reached by non-football content today, so fixing
it would expand the diff with no behavioral payoff. `_is_shallow_beat()`'s
`bool(stub.player)` requirement is also left untouched — loosening it to gate non-person
beats into enrichment for non-football niches is a separate, bigger design question the
architecture review didn't raise.

## Test strategy

Original 6 tests: 5 in `tests/test_enrichment.py` (system-prompt selection x2,
tactical-regex selection x3, isolating the "neither vocabulary matches" path from the
"too short" path `_is_shallow_beat()`'s `len() < 25` OR'd condition already covers) and 1 in
`tests/test_generate_task.py` closing the standard-path wiring gap
(`_enrich_standard_path_guide()` had zero direct tests). Mutation-tested: hardcoding the
vocabulary selection and hardcoding the standard-path call site each failed the right test.

The review round added 17 more (see §Corrections): `_enrich_with_insight()` end-to-end for
both vocabularies and across multiple batches, `_is_football_niche()`/`_clean_niche()` unit
tests, the empty/hostile-niche prompt tests, and a parametrized test of
`_generate_from_structured_script()` itself (niche "personal finance", `None`, `""`,
"Premier League football") running the real `_enrich_with_insight()` through a capturing
LLM. 855 default-run tests pass (856 including the deselected golden test); 5 mutations
(wrong niche to the shallow gate, wrong niche to the batch prompt, hardcoded structured-path
niche, substring→exact match, no cleaning) each fail at least one test.

## Corrections

A deep 4-persona review on the opened PR (#36) found no security defect and these real
issues, all fixed before merge:

1. **Behavior regression on the structured path (3 of 4 personas independently).** The first
   version copied `evaluator.py`'s exact-set-membership check (`niche.lower() in
   {"football","soccer","futbol"}`) and passed `reel.niche or ""`. The structured-script path
   is football-shaped by design and `Reel.niche` is an optional free-text field, so a football
   script with a blank niche, or "Premier League football", silently moved from the football
   prompt/vocabulary (the module's only behavior before this PR) to the generic branch. The
   design's own claim that the structured path was "unaffected" was wrong for exactly this
   input. Fixed: `_is_football_niche()` treats an unset/blank niche as football and matches by
   substring. This deliberately diverges from `evaluator.py`'s exact match; that axis has the
   same limitation but was not touched here.
2. **Empty niche produced a malformed prompt** ("content analyst for a  video") — closed by (1).
3. **Unsanitized niche in a system prompt** (Security, Low): the same exposure `prompt.py`
   already has, so no new capability, but cheap to close — `_clean_niche()`.
4. **Test gaps (Test-Quality, mutation-confirmed):** `_enrich_with_insight()` had no direct
   test (dropping its niche from either callee passed everything), the structured-path wiring
   and `reel.niche=None` were untested, and the defaulted `niche=""` parameter is what made
   a silently dropped niche possible — it is now required. Closed by the 17 new tests.
5. **Documentation (Docs):** CLAUDE.md claimed "3 pre-existing `reel.niche or ...` guards"
   (main has 5: three `or ""`, two `or "general"`); docs/architecture.md still described
   `beat_enrichment.py` as structured-path-only and listed 15 enrichment tests, and two docs
   still said standard-path insight enrichment was not wired (it has been since Phase 3.6,
   roadmap §5e) — all corrected.
6. **Not changed (inherited):** `_INSIGHT_TACTICAL_UNIVERSAL` is itself sports-flavored
   vocabulary, so a non-football beat's enriched sentence rarely matches it and the `enrich`
   StageEvent's `enriched_beats` reads ~0 even on success — the same weakness `evaluator.py`'s
   Insight Density axis already has.
