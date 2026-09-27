# System design: reaper resumes killed jobs for idempotent job types — SYSTEM_DESIGN_SPEC

Status: **Ready for implementation planning**
Source item: `docs/roadmap.md` Open Issues table — `Reaper does not resume killed jobs`
(Medium severity)

**Revision note (read this first):** the original draft's central safety argument (§2) proved
the wrong thing — that a *truly dead* job's terminal mutations are never durable — and never
addressed the actual hazard: the reaper's only signal for "dead" is a stale heartbeat, which a
**live** process can also produce (a slow LLM call combined with an independent DB blip in the
heartbeat thread — a scenario this codebase's own risk model already treats as ordinary, not
exotic). Resuming under that false positive would let two executions of the same job run
concurrently and **defeat `job_task`'s existing `JobLost` fencing**, because `status='running'`
stops being a reliable "who owns this" signal the moment resume can re-open a job id a live process
still holds. An independent adversarial review caught this, plus an understated cost-multiplication
risk for `generate_guide` and a `job.meta` staleness bug that would corrupt
`estimate_generation()`'s cost-history bucketing. This revision adds a **fencing token**
(`Job.claim_token`) as a structural fix — not a probabilistic mitigation — makes the
`generate_guide` cost arithmetic explicit with a differentiated, lower resume budget, and adds an
explicit `job.meta` reset requirement.

**Second revision note:** a follow-up independent review of the fencing-token fix itself (before
any code existed — this caught the gap at the design stage) found the first cut of the fencing
mechanism was **incomplete**, not wrong in concept: it gated only three of five checkpoints that
actually need it. Two more real gaps, both now folded into §2–§4 below:
1. `_settle_failure`'s retry-reset CAS and `_fail_job`'s fail-CAS (both reachable by a zombie whose
   run has *not yet* hit any of the original three token-gated checkpoints) were left ungated. A
   zombie hitting a transient exception after being superseded could reset the *resumed* run's row
   back to `pending` and schedule a **third** concurrent execution via its own `self.retry()`, or
   flip it straight to `failed` and roll back the owner while the resumed run is still legitimately
   working — reopening the exact concurrent-execution hazard the token exists to close, and doing
   so *worse* than the original bug (silently discarding a legitimately-completed resumed run, not
   just wasting duplicate compute).
2. `_heartbeat_loop`'s own background-thread `_advance()` call (a third, independent
   `WHERE status='running'` CAS site, separate from the `heartbeat()` function the first revision
   token-gated) was left ungated. A superseded zombie's orphaned background thread would keep
   refreshing `heartbeat_at` on the resumed run's row indefinitely, masking real staleness
   detection for up to the task's full `max_runtime_s` if the *resumed* run itself later got stuck.

See §2, §3, §4, §9 for the reworked content — five checkpoints are now token-gated, not three.

---

## 1. Problem and non-goals

**Problem, quoting the roadmap's own description:** *"Under prefork, `pkill`/SIGKILL kills the
child without unwinding: the job stays `running`, the redelivered message no-ops, and the
reaper fails it after 5 minutes. Nothing re-runs it, so an enrich/generate job loses its (paid)
work and a `failed` reel has no retry endpoint."*

Confirmed against the real code: `job_task`'s own docstring (`worker/tasks/common.py`) states
this precisely — *"A child killed with SIGKILL never gets here [the `except BaseException`
handler]; its job stays `running` and the reaper fails it after `STALE_MINUTES`."* SIGKILL gives
the process no chance to run any Python exception handler at all, so none of `job_task`'s
otherwise-careful shutdown machinery (`_fail_interrupted`, the `except BaseException` branch)
ever executes — the job simply sits at whatever status its last **commit** left it in until
`reap_stuck_jobs` (`worker/tasks/maintenance.py`) notices the stale heartbeat and marks it
`failed`, rolling the owner (`Reel`/`Cut`) back to a retryable state. For `render` and `publish`,
that rollback (`draft`, `approved`) genuinely is retryable — the operator has a button. For
`enrich` and `generate`, `JOB_IN_FLIGHT` rolls the reel back to `"failed"` and
`REEL_TRANSITIONS["failed"] = {"draft"}` — the *reel* can go back to `draft`, but nothing in the
UI re-triggers `enrich_context`/`generate_guide` independently; those are only ever created by the
`POST /api/reels` → `enrich_context` → `generate_guide` chain. A reel whose enrich/generate job
was SIGKILLed is stuck needing the operator to start over from context re-entry, silently losing
whatever paid LLM work had already happened.

**Non-goals, decided explicitly, not by omission:**
- **`publish` is never auto-resumed.** `publish_cut`'s own `max_retries=0` comment already states
  why: *"publishing is an irreversible external side effect. A transient error can arrive AFTER
  the platform accepted the upload, and a retry would post the video twice."* A SIGKILL mid-upload
  is the same hazard, worse. This is a hard exclusion, not a scope simplification — see §4 for how
  it is enforced structurally, not just by omission from a dict.
- **This only covers `running`-stale jobs**, not the reaper's other two candidate classes
  (`pending`-stale — message lost before any worker picked it up — and `done`-orphan — the
  `after_commit_failed` gap `docs/roadmap.md`'s Phase 3.9 section already documents). Extending
  auto-resume to either is a separate, later scope decision.
- **This does not change task-level transient-retry semantics** (`should_retry()`/`self.retry()`,
  each task's own `max_retries`). Reaper-resume only ever fires for the one failure mode
  transient-retry structurally cannot address: **the process may be dead**, so there is no `self`
  to call `.retry()` on. (Note the hedge — "may be": this is exactly the ambiguity §2 exists to
  resolve safely rather than assume away.)
- **This does not attempt to eliminate the false-positive stale-detection window itself** (e.g. by
  raising `STALE_MINUTES` or adding a second confirmation pass). §2's fencing-token fix instead
  makes that window **safe to be wrong about** — a false-positive resume wastes duplicate compute
  and, for a paid call, duplicate cost, but can no longer corrupt final state or silently double-book
  an external side effect. Tightening the detection window further to reduce how *often* this
  happens is a legitimate, independent future refinement, deliberately not bundled into this design.

---

## 2. The central hazard, and why a fencing token — not a bigger heuristic — is the fix

**What the first draft got wrong, stated precisely:** the original argument was "a job the reaper
observes as `running`-stale can never have had its terminal mutations committed, so resuming from
scratch is always consistent." That is *true*, but it silently assumes the observed process is
actually dead. The reaper's only signal is a stale `heartbeat_at`, and that signal has a real,
non-exotic false-positive mode in this exact codebase: `_heartbeat_loop` (`worker/tasks/common.py`)
runs in a background thread on its own 30 s cadence, opening a fresh `SessionLocal()` each time —
if the main body thread is genuinely alive but blocked on one long outbound call (this codebase's
own docs note LLM calls up to several minutes; `OllamaProvider` has a 360 s timeout) *while* the
heartbeat thread's own DB write independently stalls (a transient connection blip — exactly the
class of event `is_transient_error()`/`pool_pre_ping=True` already exist to tolerate elsewhere in
this codebase), `heartbeat_at` can go stale for 5+ minutes with the process still very much alive
and working.

**Why this matters specifically for *resuming*, not for the existing fail-only behavior:** today,
a false-positive stale detection is harmless — the reaper's only action is to flip the job to the
**terminal** state `failed`. The zombie process eventually calls `heartbeat()` or reaches its
done-stamp CAS, both gated on `WHERE status='running'`; since the job is now `failed`, those CASes
correctly fail to match, `JobLost` fires (or the done-stamp silently discards the result — see
`job_task`'s own "Reaped (or superseded) mid-run: the owner was already rolled back... result
discarded" comment). Wasteful, but nothing else is executing that body concurrently.

**Resuming breaks this specific guarantee.** The reaper's resume branch sets status back to
`pending`; a second worker then claims it (`pending`→`running`) and starts executing the body from
scratch — *while the original, still-alive process is still executing the same body.* When the
zombie's own `heartbeat()` next fires, its check (`WHERE status='running'`) now matches again —
not because it's still the legitimate owner, but because a *different* run's claim put the column
back to `'running'`. `JobLost` never fires for the zombie. **`status='running'` is no longer a
reliable proxy for "this specific execution still owns the job" once resume can re-open a job id a
live process still holds** — which is exactly the textbook justification for a fencing token
(the standard fix for "lease expired, but the lease-holder might still be alive" in any
compare-and-set-based ownership scheme).

**The fix: `Job.claim_token`, a monotonically increasing counter bumped on every claim into
`running`.** Each run captures the token value its own claim produced and carries it (on the
in-memory `job` ORM object — no signature change needed anywhere else) for the rest of that
execution. `heartbeat()`, `lock_job()`, and the done-stamp CAS all add `claim_token = <the value
this run captured>` to their existing `WHERE status='running'` condition. A resumed run's claim
bumps the token to a new value; the zombie's own subsequent fencing calls now carry the *old*
token, so even though `status` still literally reads `'running'`, the token comparison correctly
fails to match, and `JobLost` fires for the zombie exactly as the architecture already intends
conceptually. This converts the failure mode from **silent data corruption** (a superseded run's
writes landing as if legitimate) to **wasted duplicate work with a guaranteed-consistent final
state** (only the winning run's terminal mutation can ever land; the loser is fenced off at its
very next heartbeat/lock/done-stamp checkpoint, all of which already happen at bounded intervals —
`HEARTBEAT_INTERVAL_S` = 30 s at the outside). The latter is an acceptable, explicitly accepted
cost (see §7); the former is not, and is what shipping the original draft would have risked.

**Five checkpoints need the token, not three — a gap in the first cut of this fix, found by a
follow-up review before any code existed.** The original version of this section reasoned that
only `heartbeat()`, `lock_job()`, and the done-stamp are reachable by a live, superseded run, and
left every fail-CAS path untouched on the theory that "`failed` is terminal, so nothing needs to
protect it." That's true for **where the CAS ends up**, but wrong about **whether it can be
reached by a zombie whose run hasn't hit any token-gated checkpoint yet**. Two more CAS sites,
both inside `_settle_failure` (`worker/tasks/common.py`), are reachable exactly that way and need
the token too:

- **The retry-reset CAS** (`_settle_failure`'s `not committed and retriable` branch, an inline
  `_advance()` call setting `status='running'→'pending'`): a zombie that raises a transient
  exception (plausible — the same flaky outbound call that starved its heartbeat is a good
  candidate to eventually time out) reaches this *without* having called `heartbeat()`/`lock_job()`
  first in that run. If a resumed run has since claimed the row (`status` is `'running'` again,
  under a new token), this CAS's `WHERE status='running'` alone still matches — it flips the
  *resumed* run's row back to `pending` and lets the zombie schedule `self.retry()`, which will
  claim it a *third* time. This reopens concurrent double-execution via `self.retry()` instead of
  via the reaper — the exact hazard this whole design exists to close, through a different door.
- **The fail-CAS inside `_settle_failure`'s `not committed` / non-retriable branch** (calls
  `_fail_job(db, job_id, models.JobStatus.running, ...)`): reachable the same way, with a worse
  consequence — it flips the resumed run's row straight to `failed` and rolls the owner back
  *while the resumed run may still be legitimately working and about to succeed*. The resumed
  run's own (correctly token-gated) done-stamp CAS then fails to match once it finishes, and its
  genuinely-completed result is silently discarded (the existing "reaped (or superseded) mid-run"
  log line) — worse than "bounded wasted work," since real, successful work is thrown away for no
  reason and the operator sees a false failure.

Both are fixed the same way as the original three: thread an optional `token` parameter through
`_settle_failure` and into these two calls (see §4). **`_fail_job_keep_owner` is correctly excluded
and needs no change** — reaching it requires *this* run's own done-stamp CAS to have already
matched, which by construction means its token was current at that moment, so no zombie can ever
reach that specific branch; the original reasoning holds there, it just didn't generalize to the
other two fail-adjacent CAS sites.

**A third, separate gap in the same review pass**: `_heartbeat_loop` (the background thread
`job_task` starts per run, distinct from the `heartbeat()` function bodies call explicitly) does
its *own*, independent `WHERE status='running'` CAS on `heartbeat_at` every 30 s, on its own
freshly-opened session. The first cut of this design token-gated the `heartbeat()` *function* but
never mentioned this separate background-thread write at all. Left ungated, a superseded zombie's
orphaned thread (still alive because the zombie's *main* thread hasn't unwound yet — nothing stops
it until it independently hits a token-gated checkpoint) keeps refreshing `heartbeat_at` on the
*resumed* run's row indefinitely, masking real staleness detection for up to that task's full
`max_runtime_s` if the resumed run itself later gets stuck. Fixed by capturing `claim_token` as a
plain immutable int and passing it into the thread at start (`_heartbeat_loop(job_id, stop,
max_runtime_s, claim_token)`), gating its own `_advance()` call on it exactly like the others.

So: **five** checkpoints are token-gated, not three — `heartbeat()`, `lock_job()`, the done-stamp,
`_settle_failure`'s retry-reset CAS, `_settle_failure`'s fail-CAS (via `_fail_job`) — and
`_heartbeat_loop`'s independent background write. `_fail_job_keep_owner` and the reaper's own
resume-CAS remain untouched, for the reasons already given.

**Idempotency of the three resumable bodies, re-derived per task (this part of the original
argument holds and is unaffected by the fencing fix — it explains why re-running from scratch,
once fencing prevents corruption, is also *correct*, not just *safe*):**
- **`enrich_context`**: `transition(reel, "generating", ...)` and `db.add(generate_job)` happen
  strictly after the last `heartbeat()` commit, with no commit of their own before the function
  returns — confirmed by reading every commit point in the function. A losing (fenced-off) run's
  in-memory mutations from this point onward are never committed at all (the connection's
  transaction is abandoned), so there is no risk of a duplicate `generate_job` row or a
  double-transition even in the brief concurrent-execution window before fencing catches the
  zombie.
- **`render_cut`**: same terminal-assignment pattern (`video_path`/`thumbnail_path`/
  `rendered_pins_fingerprint`/`transition(cut, "in_review", ...)` land together at the very end).
  Two mid-body commit points exist — `resolve_or_reuse()`'s own per-beat `CutAsset` pin commits,
  and the `CutAsset` timecode `start_s`/`end_s` `.update()` shortly before `composite_cut()` — both
  already idempotent/re-computable (the former by design, for exactly the "resumable re-render"
  case; the latter is a deterministic recompute from already-measured TTS durations). A genuinely
  concurrent double-execution (before fencing aborts one) can interleave these two runs' pin writes
  for the same beats, but `assert_video_matches_pins()` (the pins-staleness gate shipped in this
  pipeline's prior fix) independently guards the one consequence that would matter — a
  video/pins mismatch reaching publish — so even a messy interleaving cannot ship a wrong video; it
  can only waste render work and, once fenced, correctly fail on the losing side.
- **`generate_guide`**: see §7 for why "idempotent to re-run" is not the same claim as
  "cost-free to re-run," which the original draft conflated.

---

## 3. Components

| Component | Change |
|---|---|
| `api/models.py` | **Two new columns** on `Job`: `reaper_resumes` (`Integer`, `default=0`, not null) — how many times the reaper has resumed this row; `claim_token` (`Integer`, `default=0`, not null) — the fencing counter from §2, bumped on every `pending`→`running` claim. |
| `migrations/versions/0013_reaper_resume_and_claim_token.py` | **New.** Both columns additive, `server_default="0"` (see §5 for why this, not a nullable "legacy" column, is the right pattern here — same reasoning applies to both new columns). |
| `worker/tasks/common.py` | **Extended to the minimum surface §2 requires — five checkpoints, not three (see §2's second revision note).** `_advance()` gains an optional `token: int | None` parameter, added to its `WHERE` clause only when supplied. `heartbeat()`, `lock_job()`, the done-stamp CAS, `_settle_failure`'s retry-reset CAS, and `_fail_job` (as called from `_settle_failure`'s non-retriable branch) all gain/forward a `token` parameter and pass `job.claim_token` when the run is `owned`. `_heartbeat_loop` gains a `claim_token` parameter, captured at thread-start time, and passes it to its own periodic `_advance()` call. The initial claim step's `_advance()` call bumps `claim_token` via a SQL-side `Job.claim_token + 1` expression (or `UPDATE ... RETURNING claim_token` via SQLAlchemy 2.0's `.returning()`, avoiding a separate `db.refresh(job)` round trip — this codebase runs SQLAlchemy 2.0.52, confirmed available), loading the real new value onto the in-memory object before anything else in `run()` uses it. `_fail_job_keep_owner` and the reaper's own resume-CAS are the only CAS sites deliberately left untouched — see §2's second revision note for why each is safe without a token. |
| `worker/tasks/maintenance.py` | **Extended.** New `_RESUMABLE_TASKS: dict[str, tuple[Celery task, int]]` allowlist mapping each resumable job type to its task callable *and* its own `max_resumes` budget (differentiated per type — see §7; `publish` deliberately absent, with a module-level `assert "publish" not in _RESUMABLE_TASKS` as a second, structural layer beyond the dict's own omission). `_reap_one()` gains a resume branch, gated on `job_type in _RESUMABLE_TASKS` and `reaper_resumes < that type's max_resumes`, that only applies to `running`-stale candidates; every other candidate (`pending`-stale, `done`-orphan, `publish`-typed, or a resumable type that already exhausted its budget) falls through to the existing fail-and-rollback path unchanged. |
| `worker/tasks/generate.py` | **Small, required change** (§7): the structured-path fallback's `job.meta` writes (`structured_score`, `structured_fallback`) must be reset at the top of every fresh invocation, not left to leak from a prior killed/retried attempt of the same `Job` row into a run that never re-triggers that branch. |
| `worker/tasks/enrich_context.py`, `render.py` | **No change.** Neither writes the kind of cross-attempt-leaking `job.meta` state `generate.py` does; §2's idempotency argument for both holds as originally written. |

---

## 4. API surface

Internal only — no HTTP-facing change, no change to any task body's own signature (`heartbeat()`/
`lock_job()` keep taking `(db, job, ...)`; the fencing token rides along on the already-passed
`job` object, invisible to every task module that calls them).

```python
# worker/tasks/common.py

def _advance(db, job_id, from_status, values: dict, token: int | None = None) -> bool:
    """Compare-and-set Job.status; when `token` is given, ALSO requires
    Job.claim_token == token — the fencing check described in the design's §2.
    Five call sites pass a token: heartbeat(), lock_job(), the done-stamp CAS,
    _settle_failure's retry-reset CAS, and _fail_job (as called from
    _settle_failure's non-retriable branch) — every CAS reachable by a run
    that has not yet had ITS OWN token checked, and could therefore still be a
    superseded zombie. _fail_job_keep_owner and the reaper's own resume-CAS
    are the only ones left untouched — see §2's second revision note for why
    each is provably safe without a token.
    """
    filters = [models.Job.id == job_id, models.Job.status == from_status]
    if token is not None:
        filters.append(models.Job.claim_token == token)
    updated = db.query(models.Job).filter(*filters).update(values, synchronize_session=False)
    return updated != 0


def heartbeat(db, job, progress: int) -> None:
    now = _now()
    if not _advance(db, job.id, models.JobStatus.running,
                    {"progress": progress, "heartbeat_at": now}, token=job.claim_token):
        db.rollback()
        raise JobLost(f"job {job.id} is no longer running (or was resumed by a fresher claim)")
    job.progress = progress
    job.heartbeat_at = now
    db.commit()


def lock_job(db, job) -> None:
    if not _advance(db, job.id, models.JobStatus.running, {"heartbeat_at": _now()}, token=job.claim_token):
        db.rollback()
        raise JobLost(f"job {job.id} is no longer running (or was resumed by a fresher claim)")


def _heartbeat_loop(job_id: int, stop, max_runtime_s: float, claim_token: int) -> None:
    """Unchanged except for the new claim_token parameter, captured once at
    thread-start (job_task passes job.claim_token when it starts this thread)
    and threaded into this loop's own periodic _advance() call — a SEPARATE
    CAS site from heartbeat() above, on its own freshly-opened session every
    HEARTBEAT_INTERVAL_S, needing its own token gate for the same reason:
    without it, a superseded zombie's orphaned thread keeps proving a resumed
    run's row "alive" indefinitely.
    """
    ...
    _advance(db, job_id, models.JobStatus.running, {"heartbeat_at": _now()}, token=claim_token)


def _fail_job(db, job_id, from_status, message: str, owner_kind: str, owner_state: str,
              token: int | None = None) -> bool:
    """Unchanged except forwarding `token` to its own _advance() call. Only the
    caller in _settle_failure's non-retriable, not-yet-committed branch ever
    passes a token; every other existing caller (the reaper's own fail path,
    _fail_interrupted, _fail_rejected_retry) keeps passing None, unaffected.
    """
    ...


def _settle_failure(self, db, job_id, exc, *, owned, committed, owner_kind, owner_state,
                    result, after_commit_failed, max_runtime_s, token: int | None = None) -> bool:
    """Gains `token`, forwarded to its two reachable-by-a-live-zombie CAS
    calls: the retry-reset (`not committed and retriable`) and the fail-CAS
    (`not committed`, non-retriable, via _fail_job). `token` is only ever
    non-None when `owned` is True — an unclaimed run never captured a
    claim_token, and its existing (unchanged) behavior needs none, since it
    was never the row's owner in the first place.
    """
    ...
    if not committed and retriable:
        ok = _advance(db, job_id, models.JobStatus.running, {
            "status": models.JobStatus.pending,
            "error": f"transient failure, retry {self.request.retries + 1}: {message}"[:2000],
        }, token=token)
        db.commit()
        return ok
    ...
    else:
        _fail_job(db, job_id, models.JobStatus.running, message, owner_kind, owner_state, token=token)


# Inside job_task's run(), the initial claim:
if not _advance(db, job_id, models.JobStatus.pending, {
    "status": models.JobStatus.running,
    "heartbeat_at": _now(),
    "claim_token": models.Job.claim_token + 1,   # SQL-side increment
}):
    db.rollback()
    return
db.refresh(job)   # load the real post-increment claim_token value onto the in-memory object —
                   # expire_on_commit=False (already used for every Job session in this file) has
                   # no effect on an explicit refresh(), which always re-fetches regardless; an
                   # equally valid alternative is UPDATE ... RETURNING claim_token via SQLAlchemy
                   # 2.0's .returning() (available — this codebase runs 2.0.52), one round trip
                   # instead of two
job.status = models.JobStatus.running
owned = True
thread = threading.Thread(target=_heartbeat_loop, args=(job_id, stop, max_runtime_s, job.claim_token), ...)
# ... job.claim_token is now available for every heartbeat()/lock_job()/_settle_failure(token=...)
# call this run makes ...

# The done-stamp CAS gains the same token=job.claim_token argument.
```

```python
# worker/tasks/maintenance.py
_RESUMABLE_TASKS: dict[str, tuple["Callable", int]] = {
    "enrich":   (enrich_context, 2),
    "render":   (render_cut, 2),
    "generate": (generate_guide, 1),   # lower budget — see §7's cost arithmetic
    # "publish" deliberately absent.
}
assert "publish" not in _RESUMABLE_TASKS, "publish must never be auto-resumed — see design §1"
```

`_reap_one()`'s new branch (illustrative, not final code):

```python
def _reap_one(db, job_id, seen_status, reason, stale_clause, job_type) -> bool:
    if seen_status == models.JobStatus.running and job_type in _RESUMABLE_TASKS:
        task, max_resumes = _RESUMABLE_TASKS[job_type]
        resumed = (
            db.query(models.Job)
            .filter(
                models.Job.id == job_id, models.Job.status == seen_status, stale_clause,
                models.Job.reaper_resumes < max_resumes,
            )
            .update({"status": models.JobStatus.pending,
                     "reaper_resumes": models.Job.reaper_resumes + 1},
                    synchronize_session=False)
        )
        if resumed:
            db.commit()   # durable BEFORE the re-enqueue — see §6
            task.delay(job_id)
            _log.warning("job %s (%s) resumed after a missed heartbeat", job_id, job_type)
            return True
        db.rollback()
    # ... existing fail-CAS unchanged ...
```

`job_type` is threaded through from `reap_stuck_jobs`'s existing candidate-building loop, snapshotted
as a plain string (`j.type.value`) at the same point `id`/`status` already are — for the same
reason the code already documents for `status` ("the session expires its instances on commit, and
a re-read status would defeat the status pin").

---

## 5. Data model

```python
# api/models.py, class Job — placed after `attempts`
reaper_resumes = Column(Integer, default=0, nullable=False, server_default="0")
claim_token = Column(Integer, default=0, nullable=False, server_default="0")
```

`migrations/versions/0013_reaper_resume_and_claim_token.py` — additive, both columns
`server_default="0"` at the DB level (not just the ORM default), directly precedented in this
codebase: `migrations/versions/0002_improvements.py` already added `nullable=False,
server_default=...` columns (`cut_assets.beat_index`/`order_in_beat` at `server_default="0"`,
`assets.safe_to_publish` at `server_default="false"`) against live, populated tables in one step,
no separate backfill migration. Both new columns here are the same shape: `0` is the only
sensible starting value for every row, old or new, unlike the last two features' `None`-means-
legacy nullable columns (`Cut.subtitle_path`, `Cut.rendered_pins_fingerprint`), where `None`
carried real, preserved meaning. A plain nullable `reaper_resumes`/`claim_token` would leave a
`NULL < 2` (falsy in SQL) footgun for every pre-migration row — silently and permanently disabling
both the resume budget check and the fencing comparison for old rows. `server_default="0"` avoids
that outright rather than working around it.

`Job.attempts`/`progress` remain `default=0` only (no `server_default`, still nullable at the DB
level) — a deliberately different, already-existing choice for a different-shaped concern (a
display counter, not a safety-critical compare-and-set key); this design does not change either.

---

## 6. Consistency and idempotency

- **Commit-before-enqueue ordering is load-bearing.** The resume-CAS (`running`→`pending`,
  `reaper_resumes+1`) must commit before `.delay(job_id)` is called — reversing the order risks a
  worker's own atomic claim (`WHERE status='pending'`) racing a not-yet-durable update from this
  transaction's point of view. This mirrors `enrich_context`'s own `after_commit` hook, which
  enqueues only after the state it depends on is durable.
- **If `.delay()` itself fails** (broker unreachable) after the CAS commits, the job sits `pending`
  with no message coming — not a new gap: `PENDING_STALE_MINUTES` (4 hours) is the existing
  backstop for exactly "reached `pending`, nothing is consuming it," needing no new code path.
- **Resume budget is atomic, not read-then-write**: the CAS's own `WHERE ... AND reaper_resumes <
  max_resumes` makes the check and increment one operation; two racing reaper passes (shouldn't
  happen given `reap_stuck_jobs` is a single beat-scheduled task, but the pattern costs nothing
  extra) cannot both succeed and over-increment past the cap.
- **The fencing token closes the concurrent-execution hazard §2 identifies, but does not prevent
  the concurrent execution from starting** — it only guarantees that once it has started, exactly
  one side's terminal writes can ever land, and the other side is provably aborted (via
  `JobLost`) at its very next heartbeat/lock/done-stamp checkpoint (≤30 s later in the common case
  of the background heartbeat thread, since that fires every `HEARTBEAT_INTERVAL_S` regardless of
  what the body itself is doing). This is the accepted trade-off: bounded wasted work, never
  corrupted final state.
- **A resumable job that exhausts its type-specific budget gets exactly today's existing fail
  behavior** — the CAS's `WHERE reaper_resumes < max_resumes` stops matching, `_reap_one` falls
  through unconditionally to the pre-existing, unchanged fail-and-roll-back-owner path.
- **No interaction with `CUT_TRANSITIONS`/`REEL_TRANSITIONS`.** A resume does not transition the
  owner — it stays exactly where it already durably is, per §2's original (still-valid) point that
  a `running`-stale job's terminal owner-transition was never committed.
- **`JOB_IN_FLIGHT` is not consulted for a resume** — only for the (unchanged) rollback path.

---

## 7. Failure strategy, cost arithmetic, and observability

**`generate_guide`'s cost-multiplication risk, made explicit (the original draft's gap):**
`_enforce_paid_call_budget()` bounds *lifetime* paid calls per reel at `Settings
.max_paid_llm_calls_per_reel` (default **20**, `api/config.py`) — but that cap is what stops a
resumed run from spending *unbounded* cost, not what makes a *single* crash-then-resume cycle
cheap. Reading `generate_guide`'s actual body: the standard path alone runs up to 3 attempts
(`for attempt in range(3)`), each attempt potentially making multiple paid calls (generation +
judge, per `CLAUDE.md`'s own documented two-tier scoring), and the structured path adds its own
enrichment/visuals/caption-hashtag calls before ever falling through to standard. A worker killed
partway through an expensive run has already durably spent (via `record_stage()`'s own
commit-per-call behavior — confirmed: it commits on every exit, success or failure) a real chunk of
that budget; a resumed run starts a **fresh** best-of-3 loop from attempt 0, potentially spending
close to the same amount again. Against a default cap of 20, a single crash-then-resume cycle for
an expensive reel can plausibly consume a large fraction of, or exhaust, the lifetime budget for a
reel that would otherwise have succeeded on its very next attempt — and `_enforce_paid_call_budget`
raising `ValueError` is a deterministic (non-retried) terminal failure, so the operator-visible
symptom would be a confusing "budget exceeded" failure on a job that "looks like" it just started.

**Mitigation adopted: `generate`'s own resume budget is capped at 1, not the shared 2** other
resumable types get (§4's `_RESUMABLE_TASKS` table). This bounds worst-case total spend across a
crash-resume cycle at roughly 2× a single run's cost rather than 3×, while `enrich` (one bounded
LLM call, cheap to re-pay per §2) and `render` (no paid calls at all in its own body) keep the
higher budget. `_enforce_paid_call_budget()` remains the actual hard backstop regardless of this
choice — the differentiated budget is calibration on top of an already-correct enforcement
mechanism, not a substitute for one.

**`job.meta` staleness across a resumed (or, latently, an ordinary retried) run — a real,
previously-unidentified bug, fixed here:** `generate_guide` sets `job.meta["structured_fallback"]
= True` and `job.meta["structured_score"] = last_score` only inside the branch where the
structured path's quality gate fails and falls through to standard
(`worker/tasks/generate.py` — confirmed at the exact lines this triggers). Because `job.meta` is
always merged additively (`{**(job.meta or {}), ...}`), never reset, a killed attempt that took
this branch and committed it (via the very next `heartbeat()` or the fall-through's own
`db.commit()`) — followed by a resumed run that succeeds cleanly on the structured path with **no**
fallback at all — would still carry the stale `structured_fallback: True`/`structured_score` from
the discarded attempt into the final, committed `job.meta`. This is not cosmetic:
`engine/generation/estimate.py::estimate_generation()` reads exactly this key to exclude
structured-fallback reels from the "standard path" cost-average bucket (per `CLAUDE.md`) — a
stale `True` would misclassify that reel's cost history, corrupting the estimate feature for every
future reel on that path. **Fix**: at the very top of `generate_guide`, before the structured-path
branch runs, strip `{"structured_fallback", "structured_score", "path"}` from whatever `job.meta`
already holds (from a prior killed or retried attempt of this same `Job` row) rather than relying
on additive merge to somehow self-correct. This closes the bug for ordinary task-level retries too,
not just resumes — resume is what turns a rare, barely-reachable edge case into an operationally
common one, which is why it surfaces here rather than being separately filed.

**Everything else about failure handling is unchanged**: a resumed job that fails again for an
ordinary (non-crash) reason goes through `job_task`'s completely normal failure path; reaper-resume
only affects the entry point (a fresh `.delay()` instead of a no-op redelivery), never the body's
own success/failure handling once it starts running.

**Logging**: each resume logs `_log.warning` naming the job id, type, and resume count vs. that
type's budget. **No new `StageEvent`** — Job-lifecycle bookkeeping, not a pipeline stage. **No UI
change** — explicit scope boundary (§9), not an oversight.

---

## 8. Capacity

Negligible beyond the original draft's estimate. The fencing token adds one extra `db.refresh(job)`
per claim (one SELECT, at the exact point the code already mirrors CAS results onto the in-memory
object) and one extra `WHERE` predicate on three already-existing, already-indexed-by-primary-key
queries (`heartbeat()`, `lock_job()`, the done-stamp). No new query shape, no new index needed.

---

## 9. Rollout plan

Single PR, no phased flag — additive columns with safe defaults; the resume/fencing machinery only
activates for a narrow, currently-broken case with no existing behavior to regress.

1. Migration `0013_reaper_resume_and_claim_token.py` (`Job.reaper_resumes`, `Job.claim_token`,
   both `server_default="0"`), verified against real Postgres (upgrade/downgrade/upgrade).
2. `worker/tasks/common.py`: `_advance()`'s optional `token` parameter, threaded into all **five**
   checkpoints identified in §2's second revision note — `heartbeat()`, `lock_job()`, the done-stamp
   CAS, `_settle_failure`'s retry-reset CAS, and `_fail_job` (via `_settle_failure`'s non-retriable
   branch) — plus `_heartbeat_loop`'s own independent periodic write, which gains a `claim_token`
   parameter captured at thread-start. The initial claim bumps `claim_token` and loads the real
   value onto the in-memory object before anything else in the run uses it. **This is the
   highest-stakes, most heavily-tested file in the repo (122 tests in `tests/test_job_lifecycle.py`
   alone) — treat every change here as requiring the same mutation-testing discipline that file's
   own history already demonstrates.** New tests, all in `tests/test_job_lifecycle.py`:
   - A claim bumps `claim_token`, and the bumped value is what subsequent `heartbeat()`/`lock_job()`
     calls within that same run use.
   - **The core regression this whole revision exists for**: simulate a "resumed while still alive"
     scenario directly — claim a job (capturing token T1), then externally force a second claim
     (simulating the reaper's resume + a second worker's claim, bumping to token T2) on the same
     row, then call `heartbeat()`/`lock_job()`/the done-stamp using the *stale* T1 — assert each
     raises `JobLost` (or the done-stamp discards, per its existing "reaped mid-run" behavior),
     confirming a superseded run is genuinely fenced off even though `status` alone would read
     `'running'` the whole time. Mutation-test this by temporarily removing the token predicate
     from `_advance()` and confirming this exact test fails.
   - **The two gaps a follow-up review found in the first cut of this fix — both must be
     regression-tested directly, not just re-derived by re-running the test above**, since the test
     above alone would not have caught either:
     1. Simulate a zombie (token T1) raising a transient exception *after* a second claim has
        landed (token T2) — drive it through `_settle_failure`'s retry-reset branch with `token=T1`
        — and assert the CAS does **not** match (0 rows updated), so the resumed run's row is left
        untouched rather than being bounced back to `pending` out from under it. Repeat for the
        non-retriable path through `_fail_job`.
     2. Start (or directly invoke one iteration of) `_heartbeat_loop` with a now-stale `claim_token`
        after a second claim has landed, and assert its write does **not** advance `heartbeat_at` —
        proving a superseded background thread can no longer mask staleness detection for the row
        it no longer legitimately owns.
   - An ordinary single-execution run (no resume involved) is completely unaffected — every
     existing test in this 122-test file must still pass unmodified, proving the token addition is
     invisible to the common case.
3. `worker/tasks/maintenance.py`: `_RESUMABLE_TASKS` (with per-type budgets and the
   `assert "publish" not in ...` guard), `_reap_one()`'s resume branch. Extend
   `tests/test_maintenance.py` (existing in-memory-SQLite `factory` fixture):
   - A stale `running` `enrich`/`render` job under its budget (2) resumes; `generate` resumes only
     once (budget 1) then fails on a second stale detection.
   - A stale `running` `publish` job is **never** resumed regardless of `reaper_resumes` — the one
     mutation-test-worthy negative case here, given §1's exclusion is a hard safety requirement.
   - `pending`-stale and `done`-orphan candidates are unaffected — confirm existing tests pass
     unmodified.
   - Commit-before-enqueue ordering: assert the CAS is durably committed before the mocked
     `.delay()` is invoked.
4. `worker/tasks/generate.py`: the `job.meta` reset at function entry (§7). New regression test in
   `tests/test_generate_task.py` (which already has two dedicated regression tests for exactly this
   class of `job.meta`-across-retry bug — the natural home): a prior attempt's committed
   `structured_fallback`/`structured_score` do not leak into a subsequent clean structured-path
   success's final `job.meta`.
5. Docs: `CLAUDE.md` (a new Key conventions entry covering §2's fencing-token mechanism and why it
   was needed — this is exactly the kind of "non-obvious, would silently reintroduce a corruption
   bug if a future change removed the token check" rule this file exists to preserve — plus the
   module-layout/data-model entries), `docs/roadmap.md` (mark the Open Issues row resolved,
   including a note on the design revision this review round required).
6. Independent review pass, focused specifically on: (a) re-deriving the fencing-token mechanism
   against the actual merged diff and running the "resumed while still alive" regression test
   personally, not trusting that it exists; (b) confirming `generate`'s lower resume budget and the
   `job.meta` reset both landed exactly as specified; (c) confirming `publish` is structurally
   unreachable (both the dict omission and the module-level assert); (d) the commit-before-enqueue
   ordering and the existing per-candidate `try/except` in `reap_stuck_jobs` still correctly
   isolates a `.delay()` failure without corrupting the already-committed resume-CAS or skipping
   later candidates in the same pass.

---

## Readiness verdict

**Ready for implementation**, after two rounds of adversarial review that each found a genuine
correctness gap, not a stylistic one — and both rounds happened before any code existed, at the
design stage where fixing them is cheapest. Round one: the original draft's central claim proved
the wrong invariant (dead-job consistency, not live-job fencing), fixed with a structural
fencing-token mechanism rather than a probabilistic mitigation, plus an explicit
`generate_guide` cost-multiplication fix (a differentiated, lower resume budget instead of
asserting cost-safety by reference to an unrelated cap) and a `job.meta` reset closing a real
data-corruption path into `estimate_generation()`'s cost-history feature. Round two, on the
fencing-token fix itself: the first cut gated three checkpoints when five needed it —
`_settle_failure`'s retry-reset and fail-CAS calls, and `_heartbeat_loop`'s independent
background-thread write, were both reachable by a live, superseded run and could have reopened
concurrent double-execution (worse, in the `_settle_failure` case, than the original bug — it
could discard a legitimately-completed resumed run's work, not just waste duplicate compute). All
five fixes across both rounds are structural — each closes the specific mechanism its review
identified, not just reduces its probability, and §9's test plan now names both of round two's
gaps as required, not-yet-existing regression tests rather than leaving them implicit.
