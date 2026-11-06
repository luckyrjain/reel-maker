# Implementation plan — `safe_fetch` and `media_policy`

Spec: `docs/specs/2026-11-safe-fetch-media-policy-module-design.md` (revision 7, six review rounds). Branch
`c2-safe-fetch-media-policy` in `/Users/luckyjain/Projects/reel-maker`, base `main` = `d096524`. Single repository,
single PR, commits 0–3 of spec §6. Tasks are sequential: each changes `asset_sourcer.py` or the tests that pin it, so
they do not parallelize safely (two agents editing the same files would conflict).

## Ground rules (every task)

1. Interpreter and tests: `/Users/luckyjain/Projects/reel-maker/.venv/bin/python -m pytest` from the repo root
   (never a bare `pytest`). Default run baseline: **2760 passed, 3 skipped, 1 deselected**. Lint gate:
   `/opt/homebrew/bin/ruff check --select F,E9 .` (the venv has no ruff). Run the full suite before every commit.
2. Stage **explicit files only** (`git add <path>`). Never stage `.gitignore` (a local tool edits it), `.ignore`,
   `.mcp.json`, `.claude/`, `graphify-out/`.
3. Do not touch `engine/publish/gate.py`, `worker/tasks/common.py`, `api/`, migrations, or any file not named in a task.
4. Pure move: no behavior change beyond spec §5. Moved code is cut and pasted, then renamed per §4.1; do not
   reformat, reorder (relative definition order is fixed, spec §4.1), simplify or "improve" it.
5. Commit trailer: `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Do not push or open a PR (T6 does).
6. There is no engineering-profile file; these rules and the task text are the profile. Report truthfully; the
   controller re-runs every verification itself.

## Task graph

```
T1 ──► T2 ──► T3 ──► T4 ──► T5(review loop) ──► T6(PR)
oracle+  commit 1   commit 2   commit 3
fidelity (move+     (retarget, (guards, probes,
script   bridge)    drop bridge) docs)
```
W1 = T1, W2 = T2, W3 = T3, W4 = T4, W5 = T5, W6 = T6, strictly sequential.

### T1 — oracle and fidelity script (belongs to commit 1; spec §4.5, §6 "Fidelity checks")
Files (new): `tests/_seam_oracle.py`, `docs/specs/2026-11-safe-fetch-media-policy-fidelity.py`.
- `_seam_oracle.py`: `MOVED` (58 baseline names → `(owner, new_name, kind)`, kinds per spec §4.5: `callable`,
  `constant` for exactly `MAX_API_JSON_BYTES` and `RENDER_ASSET_BUDGET_S`, `data` otherwise), `NEW = {"now": …}`,
  `BRIDGE_EXTRAS = {"httpx", "threading", "socket"}`, `STAYS` (every non-moved baseline top-level name plus
  `settings`, `os`, `Path`, `time`, `record_stage`). Exact spelled-out literals, no globs.
- Fidelity script (`python docs/specs/2026-11-safe-fetch-media-policy-fidelity.py [--baseline REV] [--stage 1|2|3]`;
  inserts the repo root into `sys.path`; F/E9-clean; reads the baseline with `git show REV:path`): oracle
  self-checks (MOVED derived from the baseline AST equals the literal, `MOVED ∩ STAYS = ∅`, kinds derived,
  owners define their new names), check A, check B, and (stage ≥ 2) check C, the dependency check. Print one line per
  check and exit non-zero on any difference.
- Verify: run the oracle self-checks against the baseline (the new modules do not exist yet, so owner checks
  are skipped at stage 0); script lints clean. Commit nothing yet (T2 commits both files together with the move).

### T2 — commit 1: move with a forwarding bridge (spec §6 "Commit 1", D4, D5)
Files: new `engine/render/safe_fetch.py`, `engine/render/media_policy.py`, `tests/test_bridge_forwarding.py`;
edit `engine/render/asset_sourcer.py`. **No existing test edited. `worker/tasks/render.py` unchanged.**
- Create the two modules per spec §4.1 tables: moved definitions in baseline relative order, renames per the tables,
  docstring/comment edits of spec §5 delta 2 (including the two new module docstrings), `now()` after `_monotonic`,
  `_log = logging.getLogger("engine.render.asset_sourcer")` right after the imports with a "temporary until commit 2"
  comment. Imports decided by `ruff F401/F821`; Pillow stays function-local.
- `asset_sourcer.py`: delete the moved definitions; import `from engine.render import media_policy, safe_fetch`;
  rewrite every call site to `safe_fetch.x(...)` / `media_policy.x(...)` (module-attribute form only, D3);
  `_monotonic()` → `safe_fetch.now()`; the six D1 constants stay with a one-line pointer comment; drop the ten
  imports of the §4.1 removal set; append the bridge: a `ModuleType` subclass assigned to
  `sys.modules[__name__].__class__` with a literal table built from `MOVED ∪ BRIDGE_EXTRAS` (extras forward to the
  same-named attribute of `safe_fetch`); `__getattr__` raises `AttributeError` for non-table names.
- `tests/test_bridge_forwarding.py` as in spec §6 (oracle-driven; all four patch protocols per entry; `__dict__`,
  `AttributeError`, `from … import`, non-table `setattr`/`delattr`).
- Verify (controller re-runs): full suite = 2760 passed + N bridge tests, 3 skipped, nothing in
  `git diff --name-only d096524 -- tests/` except the new bridge test and `_seam_oracle.py`; ruff clean; fidelity
  `--stage 1` checks A and B pass (the listed docstring edits are the only differences); run control-wiring
  mutants C11 (the four adapter `now()` sites) and one of each of C5, C9, C10, C12–C16 against the bridge tree and
  record that each turns an existing test red; bridge/oracle mutants B1–B8 each fail the bridge test.
- Commit: `refactor: move the hardened fetch and the media policy out of asset_sourcer (bridge, no test edited)`
  with the proof commands and results in the body. Includes `_seam_oracle.py` and the fidelity script.

### T3 — commit 2: retarget the tests, drop the bridge (spec §6 "Commit 2")
Files: edit `tests/**` (the 15 `AS.`-using files plus `conftest.py` and the `_MOD`-importing files),
`worker/tasks/render.py`, `pyproject.toml` (marker texts), `engine/render/asset_sourcer.py` (delete bridge),
`engine/render/safe_fetch.py` / `media_policy.py` (`getLogger(__name__)`); delete `tests/test_bridge_forwarding.py`.
- Write the source-position rewrite as a throwaway script (scratchpad, not committed) driven by `_seam_oracle.py`;
  forms and specifics exactly as spec §6 Commit 2 (attributes incl. tuple-assignment Store, setattr/patch.object
  names, string and f-string targets, `_FETCH_MOD`, `tests.test_sourcer_selection` import lists, `httpx` bare
  targets, `AS.threading|socket|httpx` bare names, unused `AS`/`_MOD` import removal incl. function-local ones).
- By hand: the 22 `caplog.set_level(…, logger=AS.__name__)` uses (the INFO "Downscaled" test → `media_policy`;
  record the full list for the PR); the two `worker.tasks.render._asset_clock` patch deletions in
  `tests/test_render_task.py` (show the asserted deadlines unchanged); conftest's five `raising=False` flips;
  `_wikipedia_downloads_via_get_fakes` → `safe_fetch._http_stream`; fixture/test docstrings; `render.py` →
  `safe_fetch.asset_budget|RENDER_ASSET_BUDGET_S|now`.
- Allowed test edits are exactly the list in spec §6 Commit 2; no assertion changes, none added, none deleted
  (except the bridge test).
- Verify: full suite = **2760 passed, 3 skipped**; ruff clean; fidelity `--stage 2` A, B (now including
  `render.py`) and C pass; sorted node-id set equal to the baseline's; per-file `assert`/`raises`/`approx` counts
  equal; no `AS.<moved name>` left (`grep`); re-run control-wiring mutants C1–C24 and fidelity mutants F1–F20 on the
  retargeted tree and record each result.
- Commit: `test: retarget the sourcer tests to safe_fetch and media_policy, drop the bridge`.

### T4 — commit 3: guards, probes, docs (spec §6 "Commit 3", §4.5, §8)
Files: new `tests/test_module_seams.py`; edit `CLAUDE.md`, `docs/architecture.md`, `README.md`, `CONTEXT.md`, the
fidelity script (resolver mode).
- `test_module_seams.py`: G1–G6 with synthetic-violation and positive-control tests per rule, seam probes,
  consumer-level fixture-effectiveness tests, `now()` test, logger-name tests (spec §6).
- Run the fidelity script's resolver mode; record the per-form G1 counts in spec §10 and assert them exactly in
  G1's positive control; cross-check the resolver on the baseline tree (`git show d096524:`) against the counting
  script's regex totals.
- Docs per spec §8 (grep each file for the moved names and fix every hit; module-layout entries, Key-conventions
  rewrite with a rename map, test-list entries, totals, Build-phase 8g entry; architecture.md rows; README count;
  CONTEXT.md end state).
- Fill spec §10 (baseline, fidelity output at each commit, docstring/comment edits, mutation results by id, G1 counts).
- Verify: full suite = 2760 + N guard/probe tests, 3 skipped; ruff clean; every guard mutant (Gm) red; docs
  totals match the real counts.
- Commit: `test: guard the safe_fetch and media_policy seams; document the split`.

### T5 — review loop
Four personas (security, correctness, test-quality, docs-consistency) on the finished diff `d096524..HEAD`, repeated
until a full round has zero real findings; every finding verified against the code before fixing; fixes get their own
commits (or amend the right commit while unpushed) and the full verification repeats.

### T6 — ship
Push `c2-safe-fetch-media-policy`, open the PR with the proof output, the PR-note lines of spec §5 delta 1 and §8
(logger-name change; hand-review lists: 22 loggers, `httpx`/`threading`/`socket` retargets), and the Phase 8g
summary. Check CI, offer Auto-fix per the ccd_pr tools. **Do not merge** (merge only on "check ci and merge").

## Verification matrix

| Check | After T2 | After T3 | After T4 |
|---|---|---|---|
| Full suite | 2760 + N bridge, 3 skipped | 2760, 3 skipped | 2760 + N guards, 3 skipped |
| `ruff check --select F,E9 .` | clean | clean | clean |
| Fidelity A / B / C | A, B | A, B (+`render.py`), C | all re-run |
| Bridge present | yes | no | no |
| Existing tests edited | none | exactly the §6 allow-list | none further |
| Mutants | B1–B8, part of C | C1–C24, F1–F20 | Gm1…, kind flip |
