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

New `tests/test_niche.py` (22): `clean_niche()` (moved from `test_enrichment.py`),
`is_football_niche()` parametrized for football-like / blank / other niches, an evaluator
test on a fixture whose football-vs-universal score differs ("Premier League football" scores
identically to "football" and differently from "personal finance"), and a
`_build_scoring_context()` test asserting `tactical_re`, `actions_re` and `context_re` switch
together. One wiring test in `test_generate_task.py` for the `"general"` default. Mutation-tested
(8): dropping `or "general"`, the evaluator reverting to exact match (and, separately, only
`tactical_re` reverting), the shared helper going exact, the shared helper treating blank as
football, `beat_enrichment` dropping its blank rule, `NICHE_MAX_LEN` changing, and the blank
rule using a raw strip instead of `clean_niche` — each fails at least one test. 879 default-run
tests pass.

## Corrections

A 4-persona review on the opened PR (#37) found no security or correctness defect. Real issues,
all fixed:

1. **Test gap (Test-Quality, mutation-confirmed):** the discriminating evaluator fixture's
   Score breakdown was identical for football and finance on the Insight axis (`insight:-13`
   both), so reverting only `tactical_re` to the exact-match survived. The fixture comment
   wrongly claimed "gegenpressing" discriminated it. Closed by a `_build_scoring_context()` test
   that pins all three regexes; the comment is corrected.
2. **Minor test gaps:** `NICHE_MAX_LEN` was compared to itself (now `== 64`); the
   `beat_enrichment` blank rule was untested for control-character-only niches (now
   parametrized with `"\x00"` and `"\n\t\x07"`).
3. **Docs:** `docs/architecture.md`'s tests header (47 files / 821 tests) and missing rows were
   stale; the Phase 7u design doc still described `_clean_niche()` in `beat_enrichment` and said
   the evaluator was untouched (now carries a pointer here); the `evaluator.py` docstring still
   read as an exact list.
4. **Documented, not changed:** on the structured path with no `reel.niche`, enrichment treats
   the niche as football while `score_guide()` sees `guide.niche == "general"` and uses the
   universal vocabulary — pre-existing, soft (mildly under-credits football insight), and
   intentional per the structured path being football-shaped by design.
5. **Observations, not defects:** the accented "fútbol" is not matched (no diacritic folding);
   a keyword after character 64 is not seen; the substring match is broad ("non-football",
   "soccer mom" count as football) and fails toward under-scoring, not inflation (Security
   measured a worst-case 23 rule points, at most ~9 combined, on crafted football-heavy VO).
