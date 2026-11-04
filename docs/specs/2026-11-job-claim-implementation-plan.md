# Implementation plan — the job `Claim`

Status: executed on branch `refactor/job-claim` (commit 1 `9118970`, then the step-2 commit; see `git log`). Outcomes are
recorded in spec §10, in prose rather than tables; T7 (review loop) and T8 (ship) are tracked in the pull request. The
task descriptions below are the forward plan as written; the code passes `claim=claim` (not `claim if owned else None`)
to the recorders, which is equivalent because `claim` is `None` exactly when the run is not owned.

Spec: `docs/specs/2026-11-job-claim-module-design.md` (revision 6, approved). Worktree
`/Users/luckyjain/Projects/reel-maker-wt-c1`, branch `refactor/job-claim`, base `origin/main` = `6310fcd`.
Single repository, single PR, **two commits** (spec §3 Q1, Q9). Everything below is sequential: each task
changes `worker/tasks/common.py` or the tests that pin it, so they do not parallelize safely.

## Ground rules (apply to every task)

1. Run tests with `python -m pytest` from the worktree root (never bare `pytest`: it would import the main
   checkout). Interpreter: `/Users/luckyjain/Projects/reel-maker/.venv/bin/python`. Before relying on a run,
   check `python -c "import worker.tasks.common as c; print(c.__file__)"` prints a path inside the worktree.
2. Stage **explicit files only** (`git add <path>`). A local tool keeps editing `.gitignore` and creating
   `.ignore` in this worktree; neither is ever committed.
3. Do not touch `worker/tasks/maintenance.py`, `api/enqueue.py`, `api/models.py`, `api/state.py`, migrations.
4. No behavior change beyond the spec's §5 deltas. Do not reformat unrelated code.
5. `ruff check --select F,E9 .` must stay clean (the CI lint gate). Use the repo's `.venv/bin/ruff` if present,
   else `python -m ruff` from the main checkout's venv.
6. Prose rules: match surrounding comment density and naming; no new dependencies.

## Task graph

```
T1 ──► T2 ──► T3 ──► T4 ──► T5 ──► T6 ──► T7(review loop) ──► T8(PR)
(commit 1)  (baseline) (commit 2 code) (commit 2 tests) (docs) (mutation+SQL)
```
Waves are strictly sequential: W1 = T1, W2 = T2, W3 = T3, W4 = T4, W5 = T5, W6 = T6, W7 = T7, W8 = T8.

### T1 — Commit 1: introduce `Claim`, route heartbeat/lock_job/done-stamp/loop (spec §4.1–4.3 step-1 column, §4.5)
Files: `worker/tasks/common.py`, new `tests/test_job_claim.py`.
- Add `@dataclass(frozen=True, slots=True) class Claim(job_id, token, owner_kind, owner_state)`,
  `Claim.advance(db, from_status, values)` (calls the module-global `_advance(db, self.job_id, from_status, values,
  token=self.token)`), `Claim.for_loaded_job(job)` (classmethod: `Claim(job.id, job.claim_token,
  *JOB_IN_FLIGHT[job.type.value])`), and `_claim_of(job)` (`if hasattr(job, "_claim"): return job._claim` else the
  classmethod — a genuine `if`/`else`, never `getattr` with a default).
- `heartbeat()` / `lock_job()`: use `_claim_of(job).advance(...)`; the `JobLost` message uses `claim.job_id`; keep the
  `job.progress = …; job.heartbeat_at = …; db.commit()` tail of `heartbeat` exactly as is.
- `job_task.run()`: build `claim = Claim(job_id, claim_token, owner_kind, owner_state)` where `claim_token` is
  captured today; set `job._claim = claim` **instead of** `job._run_claim_token` (do not write both); the done-stamp
  uses `claim.advance(...)`; start the heartbeat thread with `args=(claim, stop, max_runtime_s)` (keep the thread name);
  keep passing `token=claim.token if owned else None` to `_settle_failure`, `_fail_interrupted`, `_fail_rejected_retry`
  (unchanged signatures in step 1).
- `_heartbeat_loop(claim, stop, max_runtime_s)`: uses `claim.job_id` and `claim.advance(...)` on its own session.
- Update the docstrings/comments of the functions touched so they describe `_claim`/`Claim` (full docs pass is T5).
- `tests/test_job_claim.py`: module docstring; assertion that `worker.tasks.common.__file__` is under the repo root;
  its own `factory` and `file_factory` fixtures (copy the shape from `tests/test_r3_proposed.py`); plain helpers
  imported with the flat form `from test_job_lifecycle import _make, _task, _raises, _read, _set_job, _bump_claim_token`.
  Tests **3, 4, 5, 6, 9, 10** exactly as specified in spec §6 "New tests" (commit 1).
- Verify: full default suite green; `git diff --diff-filter=MD --name-only origin/main -- tests/` is **empty**;
  ruff clean; the new tests pass; run mutations **M1–M4, M7, M10–M12, M14–M16** (apply each, the named test must fail,
  revert — spec §6 table) and M5, M6, M8, M9 against the unchanged shims. Record results.
- Commit: `refactor: add the immutable Claim for job fencing (step 1, no existing test changed)` with a body
  stating the proof commands and results.

### T2 — Baseline SQL traces (spec §6 "SQL differential")
Files: throwaway script under the scratchpad only (not committed).
- Write a script that, for the ledger scenarios (success, transient retry, hard failure, shutdown, Reject owned, Reject
  not owned, pre-claim failure, `fail_unenqueued`, after_commit failure, hook raising, heartbeat after a mid-body
  rollback, `lock_job` after a mid-body rollback, heartbeat `JobLost`, `lock_job` `JobLost`), records normalized
  `before_cursor_execute` statements plus commit/rollback events.
- Run it on `origin/main` (a clean checkout of `6310fcd` in the scratchpad), after commit 1, and (T6) after commit 2.
  Commit-1-vs-main must differ only by the §5 table deltas, and exactly there.

### T3 — Commit 2 (code): required keyword `claim` everywhere (spec §4.3 step-2 column, §4.4)
Files: `worker/tasks/common.py`.
- `_advance(db, job_id, from_status, values, *, claim)`; `Claim.advance` passes `claim=self`; the claim write passes
  `claim=None`; `_advance` raises `ValueError` if `claim is not None and claim.job_id != job_id`; it adds the
  `claim_token` predicate when `claim is not None`.
- `_fail_job(db, job_id, from_status, message, owner_kind, owner_state, *, claim)`: owner-pair `ValueError` check as the
  **first statement**; `_fail_unclaimed(db, job_id, from_status, message, owner_kind, owner_state)` calls
  `_fail_job(..., claim=None)`; `fail_unenqueued` and the not-owned `_settle_failure` branch use it.
- `_fail_job_keep_owner` calls `_advance(..., claim=None)`.
- `_fail_interrupted(db, job_id, exc, owner_kind, owner_state, *, claim: Claim)`, `_fail_rejected_retry(db, job_id, exc,
  owner_kind, owner_state, *, claim: Claim | None)` (lambda: `_fail_job(..., claim=claim)` when owned, `_fail_unclaimed`
  when `claim is None`), `_settle_failure(self, db, job_id, exc, *, claim: Claim | None, committed, owner_kind,
  owner_state, result, after_commit_failed, max_runtime_s)` with `owned = claim is not None`, the two `ValueError` checks
  at its top, the retry-reset through `claim.advance`, and the fail path through `_fail_job(..., claim=claim)`.
- `run()`: pass `claim=claim if owned else None` / `claim=claim` accordingly; `_retry_budget(self, owned)` receives
  `owned = claim is not None`.
- Remove every `token: int | None = None` parameter. Update the affected docstrings.
- Verify: the suite is red **only** at the enumerated call sites (spec §6 step 2); do not fix tests in this task.

### T4 — Commit 2 (tests): migrate the enumerated existing tests, add tests 1, 2, 7, 8
Files: `tests/test_job_lifecycle.py` (helper `_claim_for`, the 3 `_fail_interrupted` calls, the `_fail_rejected_retry`
call at `:1785`, docstring text), `tests/test_r4_gaps.py` (`:198`, `:205`), `tests/test_r3_proposed.py` (the `:286–289`
spy), `tests/test_job_claim.py` (tests 1, 2, 7, 8).
- Allowed edits to existing tests: call arguments/keywords, comment/docstring text, helper additions — **no assertion
  changes**. `_fail_rejected_retry` tests keep calling `_fail_rejected_retry(..., claim=None)`.
- Verify: full suite green; `git diff -U0 HEAD -- tests/` (before committing: against the index) shows no removed
  `assert`/`pytest.raises` line; ruff clean; mutation **M13** and re-run M1–M12, M14–M16 on the final code.

### T5 — Documentation (spec §8)
Files: `CLAUDE.md`, `CONTEXT.md`, `docs/architecture.md`, `docs/design.md`, `README.md`, the spec (§10 filled in).
- Exactly the places listed in spec §8; counts: 76 → 77 test files, default-run count = 2663 + N (N = number of new
  test functions/cases collected from `tests/test_job_claim.py`), total = default + 1 deselected.
- Update the "Fencing token" entry (eight fenced writes, `Claim`, the signature guard + ledger as the structural
  answer, the known-gaps paragraph) and the `common.py` module-layout entry; add the `tests/test_job_claim.py` entry.

### T6 — Final verification of commit 2
- Re-run the SQL differential (T2 script) on the final tree; diff against main and commit 1; paste the result into
  spec §10.
- Re-run the full mutation table on the final code; paste the table of results into spec §10.
- `pytest` full run, `ruff check --select F,E9 .`.
- Commit 2: `refactor: make the Claim the only way to fence a job write (step 2)`.

### T7 — Review loop (4 personas, repeated until a full round has zero real findings)
Security / correctness / test-quality / docs reviewers on the whole PR diff (`git diff origin/main...HEAD`), commit 2
reviewed separately as well. Fix every real finding, re-run the affected checks, repeat.

### T8 — Ship
Push, open the PR (body with the proof commands and results, trailer per repo convention), watch CI, merge when green.

## Verification matrix

| Check | When | Command / evidence |
|---|---|---|
| No existing test modified/deleted | after T1 | `git diff --diff-filter=MD --name-only origin/main -- tests/` empty |
| No behavior assertion changed | after T4 | `git diff -U0 origin/main...HEAD -- tests/test_job_lifecycle.py tests/test_r3_proposed.py tests/test_r4_gaps.py \| grep '^-.*\(assert\|pytest.raises\)'` empty (the three pre-existing files only) |
| Suite green | every task | `python -m pytest -q` |
| Lint | every commit | `ruff check --select F,E9 .` |
| Mutations M1–M16 | T1 (commit-1 subset), T4/T6 (all) | spec §6 table; results recorded in §10 |
| SQL differential | T2 / T6 | identical except the §5 deltas |
| Wrong tree guard | every run | `worker.tasks.common.__file__` under the worktree |
