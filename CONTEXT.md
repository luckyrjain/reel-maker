# Context

Project domain glossary — terms that have come up in conversation or design work on this repo and are
worth defining once rather than re-explaining each time. This file grows as sessions surface new terms;
it is not an attempt at a complete domain model in one pass. A term only earns an entry here because it
was actually used, ambiguous, or contested in a real session — not because it sounds important.

See `docs/architecture.md` for the module-by-module system breakdown, `docs/design.md` for engineering
design rationale, and `CLAUDE.md`'s "Key conventions" section for the full implementation-level account
behind anything summarized here.

---

## Job claim & fencing

Surfaced via `/domain-modeling` session on the reaper-resume mechanism (Phase 7f,
`docs/specs/2026-09-reaper-resume-killed-jobs-system-design.md`).

- **Claim** — the atomic `pending → running` transition a worker performs to take ownership of a `Job`
  row. `worker/tasks/common.py::job_task`'s `run()`.
- **Claim token** (`Job.claim_token`) — a monotonically increasing counter bumped via a SQL-side
  `Job.claim_token + 1` on every claim. Exists because `status` alone stops being a reliable ownership
  signal once a stale job can be *resumed* (put back to `pending` for a second worker) rather than only
  ever failed — two different runs can both have `status='running'` at different points in time, and
  `claim_token` is what tells "the run that currently owns this row" apart from "a run that used to."
- **Fencing** — gating a state-changing write (a compare-and-set) on `claim_token == <this run's
  captured token>`, not just `status`. A write from a superseded run fails the CAS even though `status`
  still superficially matches. `_advance()`, `worker/tasks/common.py`.
- **Resume** (reaper resume) — `reap_stuck_jobs()` returning a stale `running` job of a *resumable* type
  (`enrich`, `render`, `generate`) to `pending`, instead of failing it outright. Bounded by a per-type
  **resume budget** (`enrich`/`render`: 2, `generate`: 1, deliberately lower). `publish` is structurally
  excluded — never resumable, since resuming a killed publish risks a double-upload, an irreversible
  external side effect fencing alone can't make safe. `worker/tasks/maintenance.py::_RESUMABLE_TASKS`.
- **Zombie** — a run whose process is still physically executing but whose claim has been superseded by
  a resume (and a second worker's subsequent claim). Fenced off at its *next* heartbeat/lock/CAS
  checkpoint (bounded wasted work), not immediately — this is the central property the fencing-token
  mechanism exists to guarantee.
- **`job._run_claim_token`** — a plain, unmapped instance attribute (not an ORM column) pinning a run's
  own captured token across mid-run `db.rollback()` calls, which otherwise expire the mapped
  `job.claim_token` and silently resync a naive re-read to whatever (possibly fresher, superseding)
  value the row holds now.

See CLAUDE.md's own "Fencing token" Key-conventions entry for the full six-checkpoint, three-review-round
account of how this shipped (each round catching a genuine, distinct correctness gap in the same
mechanism) and `docs/design.md` §3 for the summary-level why.

**Related but distinct** — don't conflate with the *publish-time* staleness gates
(`Cut.rendered_pins_fingerprint` / `rendered_guide_fingerprint`, Phase 7e/7m): those fence a re-render
against a stale video/guide via fingerprint comparison, a different problem (data staleness after a
guide edit) from job-claim ownership. Both are called "staleness" informally in conversation; they are
unrelated mechanisms solving unrelated problems.
