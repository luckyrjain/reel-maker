# Implementation plan — `render_output`

Spec: `docs/specs/2026-11-render-output-module-design.md` (r4). Branch `c3-render-output` in
`/Users/luckyjain/Projects/reel-maker`, base `main` = `24cbc91`. Single repository, single PR. Tasks are sequential on one branch (T2 depends on T1; T3, T4 and T5 on T2); running them in parallel would only add
merge noise.

## Ground rules (every task)

1. Interpreter and tests: `/Users/luckyjain/Projects/reel-maker/.venv/bin/python -m pytest` from the repo root (never a
   bare `pytest`). Lint gate: `/opt/homebrew/bin/ruff check --select F,E9 .`. Baseline at `24cbc91`: 3075 selected
   (3072 passed, 3 skipped, 1 deselected). Run the full suite before every commit.
2. Stage **explicit files only** (`git add <path>`). Never stage `.gitignore` (a local tool edits it), `.ignore`,
   `.mcp.json`, `.claude/`, `graphify-out/`.
3. Touch only files named in the task. No schema, migration, route or template change.
4. Pure move: function bodies and constant values of the four moved names are cut and pasted unchanged; only docstrings
   and comments that name old homes are edited (spec §4.1).
5. Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Do not push or open a PR (T6 does).
6. Report truthfully; the controller re-runs every verification itself.

## Task graph

```
T0 baseline capture → T1 wiring test (old code) → T2 move → T3 tests+guards+battery → T4 audio_duration → T5 docs → T6 review loop + PR
```

### T0 — baseline capture (no commit)
**T0a (before T1).** Record `pytest --collect-only -q` node ids of `24cbc91` into the scratchpad (3075 selected).
Snapshot, for the normalized-diff script (spec §6.1), six of the seven test files of spec §6.2 (all but
`tests/test_render_task.py`) and `tests/_seam_oracle.py` at `24cbc91`.
**T0b (after T1).** Snapshot `tests/test_render_task.py` as of commit 1, which extends it.
Write the throwaway normalized-diff script: strip docstrings and ignore comments; flatten `ImportFrom` into
`(module, name, asname)` multisets with the four-name module remap; apply the six patch-string rewrites of spec §6.2
(assert the rewritten count is six); require `ast.dump` equality. Negative controls that must each fail: an extra edit,
a wrong-module import, a wrong-prefix patch string.

### T1 — pin the writer on the old code (commit 1)
File: `tests/test_render_task.py`. Add the §6.3 commit-1 wiring test (two distinct candidates, `out_path` read from
`composite_cut.call_args.kwargs["output_path"]`, distinct thumbnail base path, duration 17.25, non-empty black frames,
subtitle). It must pass on the unmodified writer. Mutation check on the OLD code: delete each of the `video_path`,
`thumbnail_path`, `thumbnail_candidates`, `duration_s` assignments in `worker/tasks/render.py` in turn — each must turn this
test red; revert. Commit: `test: pin render_cut's writes before moving them`.

### T2 — the move (commit 2)
Files: new `engine/render/render_output.py`; edit `worker/tasks/render.py`, `engine/publish/gate.py`,
`engine/generation/guide_schema.py`, `engine/render/asset_sourcer.py`, `api/models.py` (comments only), the test retargets
of spec §6.2, `tests/_seam_oracle.py`.
- `render_output.py` per spec §4.1-4.4: moved functions/constant, `RENDER_ARTIFACT_COLUMNS`, `RenderOutput`, `record`
  (locals first, then one assignment block), `_assert_not_stale`, `assert_pins_not_stale`, `assert_guide_not_stale`.
  The staleness messages are copied byte for byte from the old `gate.py`.
- `render.py`: build `RenderOutput` after the posted-while-rendering check, call `render_output.record(db, cut, output)`
  (module-attribute call), keep `transition(...)`; remove the eight assignments, the comment blocks that moved, and the
  imports of the moved names.
- `gate.py`: two one-line delegates via `render_output.assert_*_not_stale(db, cut)`; imports of the moved names removed;
  docstrings shortened to pointers.
- `guide_schema.py`: delete `compute_guide_fingerprint` and the now-unused `import hashlib` / `import json`.
- `asset_sourcer.py`: delete the three pins names (keep `_fp`, `hashlib`, `models`).
- Retarget tests mechanically per §6.2; amend `_seam_oracle.py` `STAYS` and its comment.
- Verify: full suite = baseline set + T1's test, ruff clean, normalized-diff script passes on every retargeted file and
  fails its negative control. Record outputs. No new tests in this commit.
Commit: `refactor: move the render-output persistence and staleness checks into render_output`.

### T3 — tests, guards, battery (commit 3)
File: new `tests/test_render_output.py` per spec §6.3 (commit 3 list) and §4.5 (G-A..G-D with synthetic controls). Run
the whole mutation table of §6.4 (R1-R39; apply each mutant, confirm the named test fails, revert) and record
mutant → failing test in the implementation record. Commit: `test: pin record, the staleness checks and the column ledger`.

### T4 — `tts.audio_duration` (commit 4)
Rename `_audio_duration` -> `audio_duration` in `engine/render/tts.py` (the def and the one internal call), `worker/tasks/render.py`
(the `tts` import line and the `_audio_duration(vo_path)` call; all line numbers in this plan and in spec §6 name the
`24cbc91` tree and move after T1/T2) and four `patch(...)` strings in `tests/test_tts.py` (:101,109,117,124). Full suite. Commit:
`refactor: make tts.audio_duration public`.

### T5 — docs (commit 5)
Everything in spec §8; fill spec §10's implementation record (baseline, normalized-diff output, T1 old-code mutation
results, battery table, final counts). Real totals only: take file and test counts from `pytest --collect-only`.
Commit: `docs: document render_output and record the evidence`.

### T6 — review loop and PR
Four personas on `main..HEAD` until a full round is clean; fixes in their own commits; re-verify after each. Then push
and open the PR (summary, proof output, review history), bind with the ccd_pr tools, offer Auto-fix. **Do not merge**
(merge only on "check ci and merge").

## Verification matrix

| Check | After T1 | After T2 | After T3 | After T4/T5 |
|---|---|---|---|---|
| Suite | 3072 + 3 skipped + 1 new | same | + new render_output tests | same |
| `ruff check --select F,E9 .` | clean | clean | clean | clean |
| Normalized test diff | n/a | all files pass, control fails | n/a | n/a |
| Mutants | T1 old-code four | — | R1-R39 | — |
