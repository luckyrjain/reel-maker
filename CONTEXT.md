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

- **Claim** — the immutable value (`@dataclass(frozen=True, slots=True)`, `worker/tasks/common.py::Claim`)
  that is one run's ownership of one `Job` row: `job_id`, `token`, `owner_kind`, `owner_state`, no field
  with a default. Created exactly once per run, by `job_task`'s `run()` right after the **claim write**,
  and pinned onto the `Job` as `job._claim`. Identity only: run phase (`owned`, `committed`, `result`)
  deliberately stays in `job_task`. The fenced write is `_advance(..., claim=<Claim>)`; `Claim.advance(db, from_status, values)` is its shorthand.
  (Phase 8f; before it, the token and these three other facts travelled as loose parameters. Not to be
  confused with the **claim write**, which this word used to name.)
- **Claim write** — the atomic `pending → running` transition a worker performs to take ownership of a
  `Job` row (`UPDATE ... WHERE status = 'pending'`, also bumping `claim_token`); the write that
  *creates* a token, so it cannot be fenced by it (the other unfenced writes are listed under **Fencing**). `worker/tasks/common.py::job_task`'s
  `run()`, `_advance(..., claim=None)`.
- **Claim token** (`Claim.token`, mirrored in the DB column `Job.claim_token`) — a monotonically
  increasing counter bumped via a SQL-side `Job.claim_token + 1` on every claim write; the `Claim` holds
  the value this run read back from the row once, and never re-reads it. Exists because `status` alone stops being a reliable ownership
  signal once a stale job can be *resumed* (put back to `pending` for a second worker) rather than only
  ever failed — two different runs can both have `status='running'` at different points in time, and
  `claim_token` is what tells "the run that currently owns this row" apart from "a run that used to."
- **Fencing** — gating a state-changing write (a compare-and-set) on `claim_token == <this run's
  captured token>`, not just `status`. A write from a superseded run fails the CAS even though `status`
  still superficially matches. `Claim.advance()` is the shorthand for the fenced write; `_advance(db, job_id, from_status,
  values, *, claim)` (`worker/tasks/common.py`) adds the token predicate when `claim` is not `None`, and
  `claim` is a required keyword with no default, so a fence cannot be dropped by omission. Eight writes
  are fenced; the unfenced ones (the claim write, `_fail_job_keep_owner`, `_fail_unclaimed` for a Job
  this process never claimed) are enumerated and pinned by tests (`tests/test_job_claim.py`); the reaper's two CAS
  sites and `enrich_context.py::_abandon_generate` are outside that scope (CLAUDE.md, known gaps).
- **Resume** (reaper resume) — `reap_stuck_jobs()` returning a stale `running` job of a *resumable* type
  (`enrich`, `render`, `generate`) to `pending`, instead of failing it outright. Bounded by a per-type
  **resume budget** (`enrich`/`render`: 2, `generate`: 1, deliberately lower). `publish` is structurally
  excluded — never resumable, since resuming a killed publish risks a double-upload, an irreversible
  external side effect fencing alone can't make safe. `worker/tasks/maintenance.py::_RESUMABLE_TASKS`.
- **Zombie** — a run whose process is still physically executing but whose claim has been superseded by
  a resume (and a second worker's subsequent claim). Fenced off at its *next* heartbeat/lock/CAS
  checkpoint (bounded wasted work), not immediately — this is the central property the fencing-token
  mechanism exists to guarantee.
- **`job._claim`** — a plain, unmapped instance attribute (not an ORM column) pinning the run's `Claim`
  (and with it its captured token) across mid-run `db.rollback()` calls, which otherwise expire the
  mapped `job.claim_token` and silently resync a naive re-read to whatever (possibly fresher,
  superseding) value the row holds now. Read through `_claim_of(job)`. It replaced the earlier
  `job._run_claim_token` (a bare int) in Phase 8f.

See CLAUDE.md's own "Fencing token" Key-conventions entry for the full account of how this shipped and
grew (a first design review and a second round that each caught a genuine, distinct gap in the same
mechanism, then later gaps through Phase 8f's `Claim`, which turned "remember to fence every write" into
a signature, a behavioral ledger and an AST guard) and `docs/design.md` §3 for the summary-level why.

**Related but distinct** — don't conflate with the *publish-time* staleness gates
(`Cut.rendered_pins_fingerprint` / `rendered_guide_fingerprint`, Phase 7e/7m): those fence a re-render
against a stale video/guide via fingerprint comparison, a different problem (data staleness after a
guide edit) from job-claim ownership. Both are called "staleness" informally in conversation; they are
unrelated mechanisms solving unrelated problems.

---

## Asset fetching & media policy

Surfaced via the C2 architecture-review grilling session (hardened fetch + image check split out of
`engine/render/asset_sourcer.py`). Module names below are the agreed targets; the code still lives in
`asset_sourcer.py` until that change lands.

- **Fetch budget** — the wall-clock limits that bound remote asset I/O, nested from small to large: a
  per-download deadline (`_VIDEO_DEADLINE_S` / `_IMAGE_DEADLINE_S`, shared by that download's redirect hops
  and 429 retry), a per-`search()` budget (`_PEXELS_SEARCH_BUDGET_S` / `_WIKI_SEARCH_BUDGET_S`), and the
  render-wide **asset budget** (`asset_budget()`, `RENDER_ASSET_BUDGET_S`, a `ContextVar` deadline for the
  asset time of one `render_cut`; TTS time is not charged to it). Inner budgets are clipped to the outer
  one. It gates the *start* of downloads and paid HuggingFace calls only; a call already in flight and the
  small API lookups are not interrupted. The mechanism (hop/read deadlines, the watchdog, the asset budget)
  lives in `engine/render/safe_fetch.py` (target module); the per-download and per-search limits are chosen by
  the adapters in `asset_sourcer.py` and passed in.
- **Media policy** — the decision applied to a downloaded (or already-cached) asset before it is trusted:
  admit it, downscale it, or refuse it. Covers the magic-byte sniff, the Pillow decode and pixel cap, the
  downscale windows, and the URL-keyed `.refused` markers for images that cannot be shrunk. A refused asset
  is never cached and, for the paid HuggingFace tiers, never billed. Lives in
  `engine/render/media_policy.py` (target module). Distinct from **safe_to_publish**, which is a *licence*
  decision enforced at publish time (`engine/publish/gate.py`), not a decision about whether the bytes are
  usable media.
