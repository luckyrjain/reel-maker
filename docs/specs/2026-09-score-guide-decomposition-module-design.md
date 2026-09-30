# Module design — score_guide() 17-axis decomposition (CAR-2)

## 1. Problem

`engine/generation/evaluator.py::score_guide()` was one ~650-line function scoring all 17 quality axes
inline against a single shared `deductions`/`issues`/`score` state, with 23 separate `score -=` sites. All
41 tests in `tests/test_evaluator.py` drove the whole function to isolate one axis's behavior — a full-repo
architecture review flagged this as CAR-2, "Worth exploring," downgraded from what line-count/test-coupling
alone would have called "Strong" by one piece of counterevidence in `docs/evaluation.md`: "A few axes
(`emotion`, `duration`, `caption_hashtag`) accumulate from more than one code path into the same key."

## 2. Re-verifying the counterevidence before designing

Direct read of `score_guide()`'s full body (before touching any code) found every one of the 17 axes lives
in its own clearly-delimited, contiguous section (each already marked by a `# ── N. <Axis Name> ───`
comment header) and writes to exactly one `deductions[key]` entry — no axis's deduction is written from two
*non-adjacent* sections of the function. Re-reading `docs/evaluation.md`'s own wording against the code
confirms "more than one code path" means multiple deduction statements *within* one axis's own contiguous
section (emotion's 4 conditional branches; duration/caption_hashtag's per-cut loop iterations across
`guide.cuts`), not literal scattering across the function. `docs/evaluation.md` itself confirms this is
harmless for extraction: "the multiplier only sees the *final* per-axis total, which is fine for scaling
purposes" — the `axis_multipliers` correction block only ever reads `deductions[key]`'s final value,
regardless of how many internal writes produced it. This reverses the original downgrade: a clean
one-function-per-axis extraction is not blocked by this counterevidence.

## 3. Design

Extracted a `_ScoringContext` dataclass — the same bundling-over-individual-threading choice
`worker/tasks/generate.py`'s `_GenerationContext` made for CAR-1, for the identical reason: 17 axes need
different, overlapping subsets of ~15 shared precomputed values (`all_beats`, `hook_beats`, `body_beats`,
`cta_beats`, `first_vo`, `all_vo`, niche-selected regexes, ...); threading them individually into 17
function signatures would reproduce the unbundled-parameter-list risk this decomposition exists to fix.
`_build_scoring_context(context, guide)` does the setup `score_guide()` used to do inline (beat
deduplication across platform guides, niche-based regex selection). Each axis became
`_score_<axis>(ctx) -> tuple[int, list[str]]`, returning exactly what its section already computed.
`score_guide()` is now a short orchestration loop over `_AXIS_SCORERS` (a `[(key, function), ...]` list)
that applies each non-zero `(deduction, issues)` to `score`/`deductions[key]`/`issues`, then runs the
existing score-overflow diagnostic and `axis_multipliers` correction block completely unchanged — both
already operate generically on the `deductions` dict, with no axis-specific logic to migrate.

## 4. Rejected alternatives

**Individually-threaded parameters per axis function** — rejected for the same reason CAR-1 rejected it:
different axes need different, overlapping subsets of the shared values, and any future new axis needing
one more shared value would require touching every other axis's call site to stay consistent.

**A merged multi-axis function for `emotion`/`duration`/`caption_hashtag`** (the 3 axes the original
counterevidence specifically named) — considered given the original review's caution, rejected after
directly confirming (§2) each is still fully self-contained within its own section. No special-casing
needed; all 17 axes share the identical `_score_<axis>(ctx) -> (deduction, issues)` shape.

## 5. Verification

All 41 pre-existing tests pass completely unmodified against the decomposed code — checked before a single
test was touched, the same discipline CAR-1 and CAR-3 both used to verify a pure refactor rather than merely
assert one. Full suite: 792 passed / 3 skipped / 1 deselected (unchanged), `ruff check --select F,E9 .`
clean. Mutation-tested: removed `_score_alignment` from the `_AXIS_SCORERS` orchestration list (simulating
the axis being silently dropped) and confirmed
`test_alignment_axis_still_penalizes_a_real_mismatch_when_a_signal_applies` fails with `assert 0 > 0` —
exactly the predicted failure — then restored and re-verified all 41 tests pass.

## 6. Migration risk

Zero external in-process callers beyond `worker/tasks/generate.py`'s 2 existing `score_guide()` call sites
(structured + standard path), both unaffected — the public `score_guide(context, guide, target_length_s,
axis_multipliers=None) -> (score, issues)` contract is unchanged. All 41 tests exercise that public contract
only; none call any per-axis internal directly, confirmed via grep before implementing.
