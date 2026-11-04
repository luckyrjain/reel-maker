# The job `Claim` — one object for the fencing identity

Status: implemented (Phase 8f), revision 6 (after spec-review rounds 1–6; round 6 found only LOW wording items, all fixed). Source: candidate C1 of the 2026-11-03 full-codebase
architecture review (strength "Strong"), grilled and settled in a nine-question session (Q1–Q9, §3).
Scope: `worker/tasks/common.py` (+ its tests and the docs that describe it). No schema change, no new
endpoint, no behavior change except the two deliberate, harmless SQL deltas in §5 and the unpinned-fallback type
lookup in §4.1.

Line numbers in this document refer to `origin/main` (`6310fcd`) unless stated otherwise; refer to code and tests by name.

Terminology used throughout (and written into `CONTEXT.md`, §8): **`Claim`** is the immutable object;
**`Claim.token`** is its value; **`claim_token`** names the `Job.claim_token` DB column (and, in the "today" columns/quotes below, the existing local
variable/parameter of that name, quoted verbatim); "the claim
write" is the atomic `pending → running` UPDATE that creates a token (`CONTEXT.md`'s current "Claim" entry defines
the transition and is split accordingly, §8).

## 1. Problem

`Job.claim_token` fences every write a run makes against a *superseding* claim (a reaper resume plus a
second worker). The token, and the other three values every failure write needs (`job_id`, `owner_kind`,
`owner_state`), travel as **loose parameters**:

| Function (worker/tasks/common.py) | Loose identity it takes |
|---|---|
| `_advance(db, job_id, from_status, values, token=None)` | `job_id`, `token` |
| `_fail_job(db, job_id, from_status, message, owner_kind, owner_state, token=None)` | all four |
| `_settle_failure(self, db, job_id, exc, *, owned, committed, owner_kind, owner_state, result, after_commit_failed, max_runtime_s, token=None)` | all four + `owned` |
| `_fail_interrupted(db, job_id, exc, owner_kind, owner_state, token=None)` | all four |
| `_fail_rejected_retry(db, job_id, exc, owner_kind, owner_state, token=None)` | all four |
| `_heartbeat_loop(job_id, stop, max_runtime_s, claim_token)` | `job_id`, token |
| `heartbeat(db, job, progress)` / `lock_job(db, job)` | token via the unmapped `job._run_claim_token`, the same `if hasattr(...)` expression written twice |

Five signatures declare `token: int | None = None`. **Because the default is `None`, forgetting the token
compiles and silently disables fencing at that write.** The recorded history of this mechanism is exactly that
failure mode. `CLAUDE.md` ("Fencing token") records "Six checkpoints carry the token, not three" (two rounds of
gaps, each found by a reviewer before it shipped), then "A fourth gap" (`heartbeat`/`lock_job` read the mapped
column) and "A fifth gap" (an eagerly evaluated `getattr` default); test docstrings number later ones
differently (a "ninth gap" in `tests/test_job_lifecycle.py`). None was found by a type error or a test written
first. `CLAUDE.md` itself concludes that "enumerating 'every CAS reachable by a live, superseded run' by hand,
twice, each found a real miss — treat any *future* addition … as needing the same explicit check". This design
moves that check into the shape of the code and adds tests that pin the exact set of unfenced `_advance` writes.

Today a *deliberately* unfenced write and a *forgotten* fence are indistinguishable in a signature.

## 2. Goals, non-goals, known gaps

Goals
1. One immutable value, `Claim`, created exactly once per run (right after the claim write) and the only way to
   make a fenced write through the lifecycle code.
2. After step 2, **no parameter that carries a fence has a default** anywhere in `common.py`; an unfenced write is
   a separately named function or an explicit `claim=None` at a call site that is enumerated and pinned by a test
   (§6), never an omitted argument.
3. One place reads the run's claim for `heartbeat()`/`lock_job()` (today: two copies of one expression).
4. Pure refactor: the same SQL statements in the same order with the same commits, the same status/ownership
   outcomes, log lines, error text and exception types — except the two deliberate SQL deltas in §5 and the
   unpinned-fallback type lookup in §4.1.

Non-goals (each deliberately out)
- Absorbing run phase (`owned`, `committed`, `result`, `stop`) into the Claim — a separate candidate (Q4).
- Moving code out of `common.py` or renaming patch targets (Q5). Tests patch, on the `common` module:
  `_advance` (14 sites), `SessionLocal` (80 sites in 8 files), `_fail_job_keep_owner` (3),
  `_fail_rejected_retry` (`tests/test_r4_gaps.py:241`), `transition` (5), `rollback_owner` (1),
  `HEARTBEAT_INTERVAL_S` (7), `threading.Thread`. All keep their names and keep being resolved as `common`
  module globals at call time.
- Changing the reaper (`worker/tasks/maintenance.py`, which imports only `_OWNER_MODELS` and `rollback_owner`
  from `common`) or `api/enqueue.py` (imports only `fail_unenqueued`) (Q2). The reaper's fail-CAS is
  authority-by-staleness, not a held token.
- Any change to `Job`, `Job.claim_token`, `Job.reaper_resumes`, migrations, `api/state.py`.
- Any change to the `heartbeat()`/`lock_job()` signatures or the 11 task-body call sites (enrich 4, render 2,
  generate 3, publish 2) (Q3).

Known gaps that this change deliberately does NOT close (so "the Claim is the only way to make a fenced write"
is not read as "every write to `Job` is fenced"; recorded in `CLAUDE.md` in the same PR):
- `job_task.run()` sets `started_at`, `attempts` and `progress` as a plain ORM attribute write (a PK-only
  `UPDATE`, no status or token predicate) and commits (`common.py` ~L743–746). A run superseded during
  `prepare()` can overwrite those three columns of the resumed run's row.
- A body's `job.meta` writes are flushed by the next `record_stage()`/`heartbeat()` commit and are not CAS-guarded.
- `heartbeat`, `_fail_job` and `_fail_job_keep_owner` mirror their CAS onto the loaded `Job` with plain attribute
  assignments (`status`, `error`, `progress`, `heartbeat_at`); those mirrors are not themselves fenced writes.
- `worker/tasks/enrich_context.py::_abandon_generate` (L36–41) performs a status-only `pending → failed` CAS on
  an orphan generate Job (a Job this run created and never claimed — it has no token to carry).
- The claim write is committed, then the token is *re-read* with `db.refresh(job)` (`common.py` ~L693–731), not
  returned by the UPDATE. A stall of more than `STALE_MINUTES` between that commit and the refresh could make a
  run adopt a sibling's token. Closing it (an `UPDATE … RETURNING`, or a token predicate on the read) changes
  SQL, so it is out of scope.
All five predate this work and need their own designs.

## 3. Settled decisions (from the grilling session)

| # | Decision |
|---|---|
| Q1 | Two steps in one PR, two commits. **Step 1**: add `Claim` and route the sites it can reach without changing a tested private signature; **no existing test is modified or deleted** (new tests may be added) — diffable proof. **Step 2**: move the remaining callers onto the Claim, make the fence a required keyword, delete the optional `token` params; small test migration (§6). |
| Q2 | `Claim` covers the owned run only. Unfenced writes become explicit, separately named paths with no `token` parameter. Reaper + `api/enqueue.py` untouched. |
| Q3 | `heartbeat(db, job, progress)` and `lock_job(db, job)` keep their signatures. The `Job` instance carries one `Claim` (attribute `_claim`) instead of an int (`_run_claim_token`). |
| Q4 | `Claim` = identity only: `job_id`, `token`, `owner_kind`, `owner_state`. |
| Q5 | Lives in `common.py`; `_advance` stays the single patchable CAS primitive. |
| Q6 | Acceptance: tests green; mutations re-run; signature guard + behavioral fence ledger; SQL differential (§6). |
| Q7 | `_heartbeat_loop(claim, stop, max_runtime_s)` receives the (immutable) `Claim` as its first positional parameter, already in step 1 (nothing outside `run()` calls it, so no int-compat branch is carried). The thread keeps its name `heartbeat-job-{id}` (tests key on it). |
| Q8 | Named `Claim`; `CONTEXT.md` + `CLAUDE.md` updated. |
| Q9 | One PR, two commits, 4-persona review of the whole PR with commit 2 reviewed separately. |

## 4. Design

### 4.1 The type

```python
@dataclass(frozen=True, slots=True)
class Claim:
    """This run's ownership of one Job row. Created once, by job_task, right after the claim write."""
    job_id: int
    token: int
    owner_kind: str
    owner_state: str
```

`token` is a plain `int` captured once from the row after the claim's `db.refresh(job)` — never re-read from the
mapped column afterwards (a `db.rollback()` expires the identity map and would resync a later read to whatever a
fresher claim holds). Frozen + slots: no body can mutate it, SQLAlchemy cannot expire it (it is not a mapped
object), and it is safe to hand to the heartbeat thread (started after `job._claim` is set; the thread never
touches the ORM `Job`). No field has a default.

Construction sites (exactly two):
- `job_task.run()` right after the claim write: `Claim(job_id, token, owner_kind, owner_state)`, where `token` is the
  value read from the refreshed row (the local that today is called `claim_token`).
- `Claim.for_loaded_job(job)` — used **only** when a `Job` has no pinned claim, i.e. a `Job` loaded and passed
  to `heartbeat()`/`lock_job()` outside `job_task` (today: only the two unpinned `lock_job` tests; no
  production caller). Builds `Claim(job.id, job.claim_token, *JOB_IN_FLIGHT[job.type.value])`. Cost relative to
  today's fallback (`job.claim_token` only): it also reads `job.type` — on an expired instance the read of
  `job.claim_token` already issues the refresh that loads every column, so no extra query — and indexes
  `JOB_IN_FLIGHT`, whose keys equal the `JobType` values (`api/state.py`). A `Job` whose type is not a `JobType`
  raises `AttributeError`/`KeyError` instead of proceeding; none exists in production, and test 3 (§6) pins the
  fallback.

### 4.2 Operations

One fenced method; every other lifecycle function takes the claim as a **required keyword** and passes it on:

| Method | Does |
|---|---|
| `Claim.advance(db, from_status, values) -> bool` | step 1: `_advance(db, self.job_id, from_status, values, token=self.token)`; step 2: `_advance(db, self.job_id, from_status, values, claim=self)` |

There is deliberately no `Claim.fail`: the failure recorders keep their `job_id`/owner parameters (the `claim=None`
paths need them) and call `_fail_job(…, claim=claim)`, so `_fail_job`'s consistency check (§4.4) fires for every
owned caller.

`heartbeat(db, job, progress)` and `lock_job(db, job)` call one private helper:

```python
def _claim_of(job) -> Claim:
    if hasattr(job, "_claim"):          # NOT getattr(job, "_claim", <default>): the default is evaluated eagerly
        return job._claim
    return Claim.for_loaded_job(job)
```

This keeps the genuine `if`/`else` (fifth-gap rule). On the pinned path nothing is read from `job` except the
pin; `heartbeat` still assigns `job.progress`/`job.heartbeat_at` afterwards exactly as today. The `JobLost`
message uses `claim.job_id` (see the deliberate delta in §5).

### 4.3 Per-site mapping (eight fenced writes + the unfenced ones)

| # | Site | Today | Step 1 | Step 2 |
|---|---|---|---|---|
| 1 | claim write `pending→running`, increments the token | `_advance(…)` no token | unchanged | `_advance(…, claim=None)` — explicit: this write *creates* the token |
| 2 | `heartbeat()` | `_advance(…, token=<pin>)` | `_claim_of(job).advance(…)` | same |
| 3 | `lock_job()` | same | `_claim_of(job).advance(…)` | same |
| 4 | done-stamp in `run()` | `_advance(…, token=claim_token)` | `claim.advance(…)` | same |
| 5 | `_settle_failure` retry-reset (owned, not committed, retriable) | `_advance(…, token=token)` | unchanged (`run()` passes `token=claim.token`) | `claim.advance(…)` |
| 6 | `_settle_failure` fail (owned, not committed) | `_fail_job(…, token=token)` | unchanged | `_fail_job(…, claim=claim)` |
| 7 | `_heartbeat_loop` periodic write | `_advance(…, token=claim_token)` | `claim.advance(…)`; signature already `(claim, stop, max_runtime_s)`, thread `args=(claim, stop, max_runtime_s)` | same |
| 8 | `_fail_interrupted` (always owned) | `_fail_job(…, token=token)` | unchanged | `_finalize_or_reconnect(db, job_id, lambda s, jid: _fail_job(s, jid, running, message, owner_kind, owner_state, claim=claim))` |
| 9 | `_fail_rejected_retry`, **owned** | `_fail_job(…, token=token)` | unchanged | lambda: `_fail_job(s, jid, pending, message, owner_kind, owner_state, claim=claim)` |
| U1 | `_settle_failure` **not owned** (`pending→failed`, "could not start") | `_fail_job(…)` no token | unchanged | `_fail_unclaimed(…)` |
| U2 | `fail_unenqueued` | `_fail_job(…)` no token | unchanged | `_fail_unclaimed(…)` |
| U3 | `_fail_job_keep_owner` (`done→failed`) | `_advance(…)` no token | unchanged | `_advance(…, claim=None)`; reachable only after this run's done-stamp CAS matched, so its token was current |
| U4 | `_fail_rejected_retry`, **not owned** (Reject on a pre-claim retry; `from_status=pending`, message "could not schedule retry: …") | `_fail_rejected_retry(…, token=None)` | unchanged | same function with `claim=None`: the same lambda goes through `_fail_unclaimed(…)`; `run()` keeps calling `_fail_rejected_retry` in both cases (no `AttributeError` on `None`) |
| — | reaper | its own CAS, `rollback_owner`, `_OWNER_MODELS` | untouched | untouched |

After step 2 the literal spelling `claim=None` exists at exactly three places: site 1, `_fail_job_keep_owner`
(U3), and `_fail_unclaimed` (the shared tail of U1, U2, U4: it calls `_fail_job(…, claim=None)`). The fence
ledger (§6) pins the resulting set of `_advance` calls.

### 4.4 Step-2 signatures

```python
def _advance(db, job_id, from_status, values: dict, *, claim: Claim | None) -> bool
def _fail_job(db, job_id, from_status, message, owner_kind, owner_state, *, claim: Claim | None) -> bool
def _fail_unclaimed(db, job_id, from_status, message, owner_kind, owner_state) -> bool   # no fence parameter
def _fail_interrupted(db, job_id, exc, owner_kind, owner_state, *, claim: Claim) -> None
def _fail_rejected_retry(db, job_id, exc, owner_kind, owner_state, *, claim: Claim | None) -> None
def _settle_failure(self, db, job_id, exc, *, claim: Claim | None, committed, owner_kind, owner_state,
                    result, after_commit_failed, max_runtime_s) -> bool     # owned == (claim is not None)
def _heartbeat_loop(claim: Claim, stop, max_runtime_s) -> None              # thread args: (claim, stop, max_runtime_s)
```

The five lifecycle functions take `claim` as a **required keyword with no default**; `_heartbeat_loop` takes it as its
first positional parameter. Forgetting it is a `TypeError` at the first execution; the only `None` is spelled out at
the three places above. `job_id` and the owner pair remain parameters
of the functions that can run with `claim=None`; where a claim is present they duplicate the claim's, so
consistency is checked with an **explicit `raise ValueError`** (not an `assert`, which `python -O` strips), and every
write path checks each fact at least once: `_advance` checks `claim.job_id != job_id` (every owned write that passes
`job_id` goes through it, so `_fail_job` does not repeat it); `_fail_job` checks `(claim.owner_kind, claim.owner_state)
!= (owner_kind, owner_state)` as its **first statement**, before its `_advance` call; `_settle_failure` also guards both
at its top, before any branch, because its retriable path goes through `claim.advance` (which uses `claim.job_id`, so
`_advance` sees neither the `job_id` parameter nor the owner pair) and its `committed=True` path never reaches `_advance`
with the claim at all. `_fail_interrupted` and `_fail_rejected_retry`
reach `_fail_job` with their own `job_id`/owner parameters, so the same check fires for them. A `ValueError` from a
recorder is a programming error, not a state to handle: it propagates to `run()`'s existing `except Exception` around
the recorder, which logs and re-raises the original error; nothing was written (the check runs before the CAS) and the
reaper's sweeps remain the backstop, exactly as for today's `OperationalError`. `_retry_budget(self, owned)` is called from
`run()` with `owned = claim is not None`. `_stamp_failed_and_run_cleanup` and `_finalize_or_reconnect` do not
change.

### 4.5 `job._claim` vs `job._run_claim_token`

`run()` sets `job._claim = claim` where it today sets `job._run_claim_token = claim_token`, and does **not** also
write `_run_claim_token` (no code or test reads it; the three hits in `tests/test_job_lifecycle.py` are docstring
text). `_claim` is a plain, unmapped instance attribute exactly as `_run_claim_token` was, so
`Session.expire()`/`rollback()`/`refresh()`/`expunge()` never touch it (only `merge` or a body reassigning `job`
would drop it — the existing rule "a body must never reassign its own `job` to a freshly queried instance" stays
and is documented next to `_claim_of`).

## 5. Behavior preservation

Unchanged: every SQL statement and its predicates (`WHERE id AND status [AND claim_token]`), the same `values`, the
same order of writes and the same commits/rollbacks; `_advance` call counts and order per scenario (several tests
count calls with `calls["n"]`); log lines; `job.error` text; exception types; `Reject`/retry flow;
`del run.__wrapped__`, `run.job_type`, `run.max_runtime_s`, the `time_limits()` wiring; thread safety (the heartbeat
thread gets an immutable value and keeps its own session/commit pattern); `expire_on_commit=False`.

**Two deliberate SQL deltas**, both in `heartbeat()`/`lock_job()` and both strictly safer (review rounds 2–3, the
exact statement traces measured with SQLAlchemy 2.0): today they evaluate `job.id` for the UPDATE and again in the
`JobLost` message. After a swallowed `record_stage()` rollback the instance is expired, so each read of `job.id`
issues a refresh `SELECT` (and on a dead connection the message formatting could raise `OperationalError` instead of
`JobLost`). With `claim.job_id`, on an expired pinned instance:

| Case | Old statements | New statements |
|---|---|---|
| `heartbeat` success | SELECT, UPDATE, UPDATE | UPDATE, SELECT, UPDATE (the one SELECT *moves*: `job.progress = …; db.commit()` flushes an expired instance, which loads its PK) |
| `heartbeat` `JobLost` | SELECT, UPDATE, SELECT | UPDATE |
| `lock_job` success | SELECT, UPDATE | UPDATE |
| `lock_job` `JobLost` | SELECT, UPDATE, SELECT | UPDATE |

Keeping `job.id` would preserve a latent masking bug, so the deltas are accepted, pinned by test 9 and expected
exactly so in the differential (§6).

Two consequences the differential (§10) measured, both the same expired-instance cause and both behavior-neutral:
(a) a `JobLost` from `heartbeat()`/`lock_job()` loses the message's refresh `SELECT` even without an earlier mid-body
rollback, because `JobLost`'s own `db.rollback()` expires the instance and the old message read `job.id` afterwards;
(b) when a body calls `lock_job()` after a mid-body rollback and then returns without anything else touching `job`, the
instance is still expired at `run()`'s ORM mirror of the done-stamp (`job.status = done; …; db.commit()`), so the
`SELECT` that `lock_job()` no longer issues happens at that final flush instead, and that mirror `UPDATE` then includes
`error = NULL` (on the freshly refreshed instance of the old code `error` was already `None`, so it was not a change).
The net statement count is unchanged and the value written is `None` either way. A third, separate behavior difference is the unpinned-fallback type lookup
(§4.1), which exists only for `Job`s that did not come through `job_task`.

## 6. Test strategy and acceptance

### Facts about the existing tests (verified)
No test calls `_advance`, `_fail_job`, `_settle_failure` or `_heartbeat_loop` directly: they appear only in
docstrings, in `patch("worker.tasks.common._advance", …)` replacements (14 sites: 7 forwarding wrappers, 6 plain
failure injectors — `return_value=False`, `OperationalError`, `ValueError` — and 1 explicit-signature spy at
`test_r3_proposed.py:300`, whose function is defined at `:286–289`). Every zombie/fencing scenario drives `run()`.
The only direct callers of the functions whose signature changes are `_fail_interrupted` at `test_job_lifecycle.py:1767, 1823,
1853` and `_fail_rejected_retry` at `test_job_lifecycle.py:1785` and `test_r4_gaps.py:198, 205`. Consequently the
step-2 signatures are covered by `run()`-level tests plus the new tests below, and the new tests are the only direct
coverage of `_advance`, `_fail_job`, `_settle_failure` and `_heartbeat_loop`.

All new tests go in a **new file**, `tests/test_job_claim.py`, importing the plain helpers (`_make`, `_task`,
`_raises`, `_read`, `_set_job`, `_bump_claim_token`) from `test_job_lifecycle` and defining its own `factory` and
`file_factory` fixtures (the repo convention: fixtures are defined per file, not imported — see the comment at
`tests/test_r3_proposed.py` ~L19–21), as `tests/test_r3_proposed.py` already does — so no test is added to an existing file (commit 2 adds only the `_claim_for` helper to `test_job_lifecycle.py`, below).

### Running the tests
Run `python -m pytest` (not bare `pytest`) from the worktree root: the editable install points at the main checkout, and
only `python -m` puts the current directory first on `sys.path`. A bare `pytest` silently tests main's code. The new
test file starts with an assertion that `worker.tasks.common.__file__` lies under the repository root it is running from.
The same applies to every mutation run and to the SQL differential.

### Step 1 (commit 1)
- No existing test file is modified or deleted: `git diff --diff-filter=MD --name-only origin/main -- tests/` is
  **empty** (only `tests/test_job_claim.py` is added).
- Default suite green (2664 + N collected, 2663 + N selected by default, 1 deselected, 0 failed, where N is the number of
  new tests; 3 optional-package skips as today); `ruff check --select F,E9 .` clean.
- Mutations applicable at commit 1: M1–M4, M7, M10–M12, M14–M16 on the new Claim code; M5, M6, M8, M9 exercise the unchanged
  shims (the `token=token` argument dropped from the shim call) and confirm they are still killed.
- New tests at commit 1: tests 3, 4, 5, 6, 9, 10 below (they test `_claim_of`, `Claim`, the pinned heartbeat loop and the
  SQL deltas, all introduced by commit 1).

### Step 2 (commit 2) — allowed edits to existing tests
Existing tests may change only in (a) call arguments/keywords, (b) comment/docstring text, (c) helper additions;
**no behavior assertion changes**. Check: `git diff -U0 origin/main...HEAD -- tests/test_job_lifecycle.py tests/test_r3_proposed.py tests/test_r4_gaps.py | grep '^-.*\(assert\|pytest.raises\)'`
(removed lines only) is empty for the step-2 commit, plus a manual read of every `+` line inside a modified hunk.
Enumerated edits: `_fail_interrupted(…)` at `:1767, 1823, 1853` (`claim=<claim>`); `_fail_rejected_retry(…, claim=None)`
at `:1785` and `test_r4_gaps.py:198, 205` (they must keep calling `_fail_rejected_retry`, not `_fail_unclaimed`,
because they exist to exercise it and `_finalize_or_reconnect`; `:1785` and `r4:205` are the unowned-Reject path U4
on a default pending job, `r4:198` is the sibling-claimed case); the `test_r3_proposed.py:286–289` spy (`token=None`
→ `claim`); `test_r4_gaps.py:241` needs no edit (the patched name survives); docstrings that cite
`job._run_claim_token` / `token=…` (≈ `test_job_lifecycle.py` L2048, L2117, L2145, L2178, L2211, L2244, L2290,
L2317–2322). New tests 1, 2, 7, 8 are added to `tests/test_job_claim.py`. A helper `_claim_for(factory, job_id)` is added to `tests/test_job_lifecycle.py` (its callers are there; the new file
imports it from there, so there is no circular import) and builds a `Claim` from the real `claim_token` read from the
row, with the owner pair from `JOB_IN_FLIGHT[job.type.value]`.

### Mutation re-run (against the final code of each commit; apply, the named test must fail, revert)
All `test_job_lifecycle.py`/`test_r3_proposed.py`/`test_r4_gaps.py` line numbers in this section are those at `origin/main` (the edited files
are slightly longer or shorter; refer to the tests by name).
| # | Mutant | Must be killed by |
|---|---|---|
| M1 | `_advance` ignores the claim's token (no `claim_token` predicate) | zombie tests for heartbeat / lock_job / done-stamp (`test_job_lifecycle.py` L2042–2160) |
| M2 | `heartbeat` bypasses the claim (token-less `_advance`) | L2042 |
| M3 | `lock_job` bypasses the claim | L2070 |
| M4 | done-stamp token-less | L2090 |
| M5 | `_settle_failure` retry-reset token-less | L2113 |
| M6 | `_settle_failure` fail-CAS token-less | L2140 |
| M7 | `_heartbeat_loop` write token-less | L2172 (+ test 6) |
| M8 | `_fail_interrupted` token-less | L2200 |
| M9 | `_fail_rejected_retry` (owned) token-less | L2234 |
| M10 | `_claim_of` reads the mapped `job.claim_token` when pinned | L2280 (rollback expiry) |
| M11 | `_claim_of` uses `getattr(job, "_claim", Claim.for_loaded_job(job))` | L2315 (refresh-event count) |
| M12 | `Claim.for_loaded_job` builds the Claim with `token=0` or a constant; or `_claim_of`'s fallback branch yields a token-less write | NEW test 3 (both halves: the bump and the control) |
| M13 | step 2: a consistency check removed from `_advance`, `_fail_job` (owner pair) or `_settle_failure` (either check, on the retriable and the committed path) | NEW test 7 (each check has at least one case that only it can catch) |
| M14 | `heartbeat`/`lock_job` still read `job.id` (UPDATE or message) | NEW test 9 |
| M15 | only one of `heartbeat`/`lock_job` bypasses the pin: reads the mapped column (killed by test 5, both functions, in the order specified there) or evaluates `Claim.for_loaded_job(job)` eagerly as a `getattr` default (killed by test 9's "no SELECT at all" for `lock_job`; the existing L2315 test covers `heartbeat` only) | NEW tests 5, 9 |
| M16 | an end-of-run write (done-stamp, `_fail_interrupted`, `_settle_failure` retry-reset or fail-CAS) builds its token/claim from the live mapped `job.claim_token` or `Claim.for_loaded_job(job)` instead of the captured `Claim` | NEW test 10 |

### New tests (all in `tests/test_job_claim.py`)
Commit 1:
3. **Unpinned fallback fences (M12).** Fresh session per call; `_make(status=running)` then
   `_set_job(…, claim_token=7)` (a distinctive nonzero token, so a `token=0`/constant mutant differs from correct).
   *Control:* load the job, do not bump; `heartbeat(db, job, n)` and `lock_job(db, job)` each succeed and
   `heartbeat_at` advances. *Fence:* a second fresh `db` loads the job at token 7, the row is bumped to 8 from another
   session (`_bump_claim_token`), then the call raises `JobLost` (one fresh `db` + loaded job per function, because
   `JobLost` rolls the session back and would expire the object). *Equality, non-tautological:* run a real task on a
   row, capture `job._claim` inside the body, then assert `Claim.for_loaded_job(factory().get(Job, id)) == captured`.
4. `Claim` is frozen and hashable; `Claim.advance` direct tests (a matching token advances; a bumped token returns
   `False` and the row is untouched).
5. **The pin is honored by both functions (M10, M15).** For each of `heartbeat` and `lock_job`, inside a task body:
   run a query, then `db.rollback()` (or `db.expire_all()`) so the mapped `job.claim_token` is expired, then bump the
   row's token from another session (`_bump_claim_token`), then call the function — with **no read or `refresh` of `job`
   in between** (a refresh between the expiry and the bump would reload the old token and let a mapped-column mutant
   still raise `JobLost`). It must raise `JobLost`. Separate steps: `job._claim` is the identical object after
   `db.rollback()`/`db.expire_all()`/`db.refresh(job)`; and the heartbeat thread receives that same object — observed
   with the existing `patch("worker.tasks.common.threading.Thread", spy)` pattern (`test_r3_proposed.py` ~L308–318,
   args arrive as `k["args"]`): select by type, `next(a for a in k["args"] if isinstance(a, Claim)) is job._claim`,
   asserted from inside the body (the thread is created before the body runs).
6. Heartbeat-loop positive control: while the claim is current `heartbeat_at` advances (patch `HEARTBEAT_INTERVAL_S`
   small in this test only, around the whole task call, as the existing tests at ~L204/L1352 do); after the token is
   bumped it stops advancing (read the "before" value only after the bump).
9. **The deliberate SQL deltas, pinned (M14).** With a `before_cursor_execute` listener, on an **actually expired**
   pinned instance (precondition asserted: `"id" not in sqlalchemy.inspect(job).dict`, reached with `db.expire_all()`
   or by running a query before `db.rollback()` — a bare rollback with no open transaction expires nothing): `heartbeat`
   success → the first statement is the UPDATE; `lock_job` success → no SELECT at all; both `JobLost` paths → no SELECT
   at all (this is what kills a `job.id`-in-message mutant). Catch `JobLost` inside the body (or detach the listener
   there) so `run()`'s own settle statements do not pollute the trace.
10. **The captured claim is used at the end-of-run writes (M16).** Parametrized over four endings of a task body that first
   runs `db.query(models.Cut).all(); db.rollback()` (so the mapped `job.claim_token` is expired) and then
   `_bump_claim_token(factory, job_id)`: `return` (done-stamp), `raise SystemExit()` (`_fail_interrupted`),
   `raise ValueError` (`_settle_failure` fail-CAS), `raise ConnectionError` with `task.retry` patched
   (`_settle_failure` retry-reset: on correct code the reset loses its CAS, so `retry` is **not** called and the raw
   `ConnectionError` propagates — assert that; the patch only matters for the mutant). After each, the row is still `running`, `claim_token == 2` and the reel is still
   `generating` — a zombie must not touch the resumed run's row. Needed because the existing zombie tests bump the token
   without expiring `job` (`expire_on_commit=False` keeps the cached value correct), so a live-read mutant at these sites
   survives them (verified in review: done-stamp and `_fail_interrupted` survived every existing test).
Commit 2:
1. **Signature guard.** For every function defined in `worker.tasks.common` (`__module__` filter), every callable in
   `Claim.__dict__` (unwrap `classmethod.__func__`; skip `member_descriptor`s and other non-callables) and
   `Claim.__init__`: no parameter whose name contains `claim` or `token`, or whose annotation mentions `Claim`, has a
   default; the five lifecycle functions' `claim` are `KEYWORD_ONLY`; `_heartbeat_loop`'s first parameter is `claim`
   with no default; for `dataclasses.fields(Claim)` check `default is MISSING and default_factory is MISSING` (the
   sentinel is `dataclasses.MISSING`, not `inspect.Parameter.empty`); the unfenced entry points (`fail_unenqueued`,
   `_fail_unclaimed`, `_fail_job_keep_owner`) have no such parameter. Lambdas and nested functions are not enumerable
   by name; they are protected by the required-keyword `TypeError` and reached by the ledger scenarios.
2. **Fence ledger (behavioral).** Spy **`_advance` only** (not `_fail_job`, which would double-count). Record
   `(from_status, frozenset(values), claim is None)` for **every** call, including a call the spy then makes raise
   (scenarios that already patch `_advance` to inject a failure — `fail_only_the_claim` style — are composed as
   record-first, then raise/forward); ignore calls on threads named `heartbeat-job-*`. For fenced calls assert
   `claim.token == 1`, the post-claim value of `_make`'s token (a constant, not read from the production `job._claim`
   and not from the row, which a `_bump_claim_token` would change); the heartbeat-`JobLost` scenario is induced by a
   status change (`_set_job(status=failed)`), not a bump. Expected `claim is None` multisets, each traced against the code: success → {claim write};
   transient retry → {claim write}; hard failure → {claim write}; shutdown (`SystemExit`) → {claim write};
   `Reject` owned → {claim write}; `Reject` not owned → {claim write (the injected failure), `pending→failed`};
   pre-claim failure → {claim write (injected failure), `pending→failed`}; `fail_unenqueued` → {`pending→failed`};
   `after_commit` failure → {claim write, `done→failed`}; `SystemExit` during `after_commit` → same; a hook that
   raises (plain) → {claim write, `done→failed` ×1}; a hook that raises **with the SAVEPOINT rollback patched to
   fail** (the technique of `test_job_lifecycle.py` ~L795–925, `patch("sqlalchemy.dialects.sqlite.pysqlite.SQLiteDialect_pysqlite.do_rollback_to_savepoint", boom)`, but driven through `run()` — a new scenario — with an
   `after_commit` that raises and an `after_commit_failed` that **writes and then raises**) → {claim write, `done→failed` ×2}; heartbeat `JobLost` → {claim write}. Every
   other `_advance` call must be fenced. The ledger observes only `_advance`; bulk updates and `.status`/`.claim_token` assignments that bypass it are covered by test 8.
7. **Consistency checks (M13), parametrized.** A mismatched `Claim` raises `ValueError`, one case per check that only that
   check can catch: `_advance` wrong `job_id`; `_fail_job` wrong owner pair (its `job_id` is guarded by `_advance`, §4.4);
   `_settle_failure` wrong `job_id` and wrong owner pair on the **retriable** path (only its own check can fire: the
   retry-reset goes through `claim.advance`, which uses `claim.job_id`) and on the **`committed=True`** path (which calls
   `_stamp_failed_and_run_cleanup` and never `_advance` with the claim); `_fail_interrupted`/`_fail_rejected_retry` with a
   wrong `job_id` or owner pair (reach `_advance`/`_fail_job`'s checks). A direct `_settle_failure` call needs a stub
   `self` with `request.retries` and `max_retries`, and an `after_commit_failed` of `None` or a no-op. Positive controls:
   a matching claim and `claim=None` do not raise.
8. **No bulk `Job` update outside `_advance`.** An AST walk over `common.py` (not a grep): outside `_advance`, fail on
   any `.query(…)` call whose arguments mention `Job` **whatever the receiver is called** (`db`, `s`, `session`, …),
   any `.update(` call carrying a `synchronize_session=` keyword, any `.bulk_update_mappings`, any `Job.__table__`, any
   `.merge(` call, any `.execute(` call, any import of `sqlalchemy.update`/`text`/`delete`, and any assignment to a
   `.status` attribute outside {`_fail_job`, `_fail_job_keep_owner`, `run`} and any assignment to a `.claim_token`
   attribute anywhere (today's only `.status` assignments are those three, `heartbeat` assigns only
   `progress`/`heartbeat_at`, and nothing assigns `.claim_token`; the enclosing scope is the **innermost** `def`, since
   `run` is nested in `decorate` inside `job_task`). (The rule is deliberately conservative:
   it must pass on today's file, where the only such `.update(` is in `_advance`; a legitimate future read of `Job`
   rows outside `_advance` would have to be added to an explicit allow-list in the test, which is the point.)
   `_abandon_generate` in `enrich_context.py` is a documented known gap, §2, as are bypasses the rule cannot see (a
   `Job` imported under an alias, `getattr(models, "Job")`, writes to other attributes such as `error`/`progress`).

### SQL differential (acceptance, one-off, recorded in §10)
Record normalized `before_cursor_execute` statements plus commit/rollback events, for the ledger scenarios above
**plus** "heartbeat after a mid-body rollback", "`lock_job` after a mid-body rollback" and the `JobLost`/zombie case
for each, on `origin/main`, after commit 1 and after commit 2 (a throwaway script, not committed), and diff the three
traces. They must be identical except for the deltas in the §5 table, which must appear exactly there.

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Most safety-critical code in the repo; a subtle change reopens the concurrent-execution hazard | Two-commit shape (no existing test modified at commit 1); M1–M16 re-run; SQL differential; independent 4-persona review of the diff |
| Tests inject failures by patching `common._advance` / `SessionLocal` / other names | They stay in `common`; `Claim` methods call them through module globals, never a bound import |
| `_claim_of` fallback differs from today's for a standalone job | Same `job.claim_token`; extra reads covered in §4.1; M12 + test 3 (with a nonzero token and an un-bumped control) pin it (the existing unpinned `lock_job` tests at L1578/L1584 do not fence — one uses a `failed` job, the other runs at token 0) |
| The guard tests give false confidence | They pin the *set* of unfenced `_advance` writes (ledger) and the bulk-update location (test 8); known gaps are documented in §2, not hidden |
| Hidden dependents of `_run_claim_token` / old private signatures | `grep`: none outside `common.py` and `tests/` |
| Rolling deploy of mixed old/new workers | No schema or wire change; no new statements; the `job.id` refresh `SELECT`s in `heartbeat`/`lock_job` are removed or (one case) reordered, §5 |

## 8. Documentation changes (part of the PR)

- `CLAUDE.md`: the "Fencing token (`Job.claim_token`)" Key-conventions entry (≈L1547–1552: the captured-int
  paragraph; "Six checkpoints carry the token" → eight fenced writes; the fourth/fifth-gap paragraph and its
  `_run_claim_token`/`hasattr` text; the regression-test paragraph; "Three CAS sites remain deliberately
  untouched" rewritten to the full list in §4.3 incl. U1/U2/U4; a new known-gaps paragraph from §2); the
  `common.py` module-layout entry (≈L226); the `test_job_lifecycle.py` entry and its "a seventh checkpoint" text
  (≈L1076, L1084); a new test-list entry for `tests/test_job_claim.py` (new file; the existing lifecycle files gain no tests, so their per-file counts stay) and the totals it changes (76 → 77 test files, the default-run count); the pytest count line (≈L51); the
  Build-phase-status paragraph.
- `CONTEXT.md`: split today's **Claim** entry (it defines the transition) into **Claim** (the immutable value; its
  fields; created once by `job_task`) and **Claim write** (the atomic `pending → running` transition that creates
  the token); **Claim token** = `Claim.token` / the DB column; replace the `job._run_claim_token` entry; update
  **Fencing** (`Claim.advance`, `_advance(…, claim=…)`) and the "six-checkpoint, three-review-round" sentence.
- `docs/architecture.md`: ≈L399 ("Six checkpoints carry the captured token" — it lists seven items and omits
  `_fail_rejected_retry`; the rewrite must not copy that), ≈L239 (the fencing regression suite description),
  totals at ≈L196 and a pointer to the new file near ≈L200 (the `test_job_lifecycle.py` row's 133 stays). `docs/design.md`: ≈L104–114 ("six separate checkpoints", "four additional gaps"; the "(133 tests)" stays) and ≈L109–110 ("three specific CAS sites deliberately carry *no* token"). `README.md`: test count.
- Code docstrings/comments that go stale: `common.py` (`_advance`, `heartbeat`, `lock_job`, `_heartbeat_loop`,
  `_fail_job`, `_settle_failure`, `_fail_interrupted`, `_fail_rejected_retry`, the `job_task` docstring and the
  capture comments in `run()`, and the `_advance` docstring's "which five call sites pass a token" cross-reference
  that `CLAUDE.md` does not contain).
- This spec: marked implemented, §10 filled in.

## 9. Open questions

None.

## 10. Implementation record

Commits on branch `refactor/job-claim`: commit 1 `9118970` (step 1) and commit 2 (step 2 + docs; `git log`).

**Commit 1 acceptance.** `git diff --diff-filter=MD --name-only origin/main -- tests/` empty; default suite 2690 passed,
3 skipped, 1 deselected (2663 + 30 new); `ruff check --select F,E9 .` clean; mutations M1–M12, M14–M16 (and M5/M6/M8/M9
on the unchanged shims) each killed by the named test.

**Commit 2 acceptance.** Default suite 2760 passed, 3 skipped, 1 deselected (`tests/test_job_claim.py` has 100 tests, after the implementation-review rounds added the owner-kind/owner-state split, job-id≠token coverage, the in-memory mirror assertion, a render-job `for_loaded_job` case and the owned-Reject dead-connection test);
`git diff -U0 origin/main...HEAD -- tests/test_job_lifecycle.py tests/test_r3_proposed.py tests/test_r4_gaps.py | grep '^-.*\(assert\|pytest.raises\)'` empty (restricted to the three pre-existing files: the only other removed assertion line is this branch's own commit-1 guard test, replaced by a module-level assertion in `tests/test_job_claim.py`); ruff clean. The existing-test edits were
exactly the enumerated ones (§6); one commit-1 test was removed and four changed: the redundant wrong-tree test (replaced by the module-level assertion), `test_advance_resolves_the_module_level_advance_at_call_time` (its spy signature moved from `token=` to `claim=`; the assertion line is unchanged), and three strengthened by the review rounds (the render-job `for_loaded_job` case, and the extra `in_transaction` and in-memory-mirror assertions in the unpinned tests). Mutations M1–M16, the M13
variants (each consistency check removed in turn, and a `_settle_failure` that checks only in the not-committed
branch), defaulted-`claim` mutants on every lifecycle function and on a `Claim` field, a fenced `_fail_unclaimed`, and a
bulk `Job` update added outside `_advance`: all killed (40 mutants, none survived).

**SQL differential** (26 scenarios incl. the ledger set, render-typed variants, zombie endings, `JobLost` with and
without a prior rollback; statements, normalized parameters, begin/commit/rollback order and final rows) on
`origin/main` (A), commit 1 (B) and the final tree (C): **B = C in all 26**; **A = B = C in 20**; the remaining six are
exactly the §5 deltas — `heartbeat` after a rollback (SELECT moves after the UPDATE), `lock_job` after a rollback (SELECT
leaves `lock_job`; it reappears at the done-stamp mirror flush, §5 (b)), and `JobLost` from either function with or without
a prior rollback (the message SELECT is gone). No UPDATE predicate, SET clause, parameter, commit or rollback changed
anywhere else.

**Review rounds.** Six spec-review rounds (four personas each; real findings 35 in round 1, then 29 → 26 → 15 → 7 → 6 for
rounds 2–6, the last round's all LOW wording items) before implementation; the implementation review rounds are recorded in the pull request.
