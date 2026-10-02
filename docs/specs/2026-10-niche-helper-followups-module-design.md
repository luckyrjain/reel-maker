# Shared niche helper + standard-path "general" default — module design (Phase 7v)

Two follow-ups left open by the PR #36 review of `beat_enrichment.py`'s niche branching
(see `2026-09-beat-enrichment-niche-branching-module-design.md`).

## Problem

1. `evaluator.py` decided "football" with an exact set match
   (`guide.niche.lower() in {"football","soccer","futbol"}`) while `beat_enrichment.py` now used
   a substring match plus an unset-means-football rule. For a niche such as "Premier League
   football", enrichment used football vocabulary and the football prompt, but scoring used the
   universal regexes — two modules disagreeing about the same niche.
2. `_enrich_standard_path_guide()` passed `guide.niche` straight through. `prompt.py` substitutes
   "general" for a blank niche, and `beat_enrichment` treats `""` as football (its structured-path
   default), so a standard-path guide with a literal blank niche would have received football
   framing — wrong for the niche-generic path.

## Decision

- New `engine/generation/niche.py` with `clean_niche()` (moved from `beat_enrichment`, constant
  renamed `NICHE_MAX_LEN`) and `is_football_niche()` (substring match of football/soccer/futbol).
  **Blank/None is NOT football in the shared helper**: `evaluator.py` scores a blank niche with
  the universal vocabulary today, and changing that was not the goal. `beat_enrichment`'s private
  `_is_football_niche()` keeps its "unset ⇒ football" rule by layering it on top
  (`not clean_niche(niche) or is_football_niche(niche)`).
- `evaluator.py` calls `is_football_niche(guide.niche)` at its single site. No import cycle:
  `niche.py` imports nothing; `beat_enrichment` already imported `evaluator`.
- `_enrich_standard_path_guide()` passes `guide.niche or "general"`.

## Behavior change (deliberate, narrow)

Scoring changes only for a niche that contains football/soccer/futbol but is not exactly one of
them (e.g. "Premier League football"): it now gets the football tactical/action/context
vocabulary instead of the universal one. No test fixture uses such a niche; exact
football/soccer/futbol and every non-football niche score identically to before.

## Test strategy

New `tests/test_niche.py` (16): `clean_niche()` (moved from `test_enrichment.py`),
`is_football_niche()` parametrized for football-like / blank / other niches, and an evaluator
test on a fixture that discriminates the two vocabularies ("gegenpressing" is football-only):
"Premier League football" scores identically to "football" and differently from "personal
finance". One wiring test in `test_generate_task.py` for the `"general"` default. Mutation-tested
(5): dropping `or "general"`, the evaluator reverting to exact match, the shared helper going
exact, the shared helper treating blank as football, and `beat_enrichment` dropping its blank
rule — each fails at least one test. 871 default-run tests pass.

## Corrections

None yet — updated after the 4-persona review on the opened PR.
