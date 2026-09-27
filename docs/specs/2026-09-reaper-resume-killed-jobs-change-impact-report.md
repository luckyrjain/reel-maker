# change_impact_report — reaper resumes killed jobs (v1)

## title
Reaper resumes killed jobs for idempotent job types (docs/roadmap.md Open Issues, Medium severity) — blast-radius analysis of the twice-reviewed, revised system design

## assessment_target
- Design: `docs/specs/2026-09-reaper-resume-killed-jobs-system-design.md` (reviewed twice, revised each round — see its two "Revision note" sections)
- Repository: `reel-maker` (single monolith checkout)
- Artifact state: `proposed_state` — no code changes exist yet; `claim_token`/`reaper_resumes` are not present in `api/models.py` or anywhere else in the current tree (confirmed by grep)

## coverage_status
**COMPLETE.** Full local repository read available; every impacted file below was read directly, including exact current line numbers for every function this design proposes to extend. No external SCM/PR context applies (no PR yet).

## criticality
**High** — elevated above the design's own implicit framing and above the two prior fixes in this same pipeline (both Medium). This is not because the *feature* itself is architecturally large (it isn't — two new columns, a handful of new parameters), but because it modifies `worker/tasks/common.py::job_task` and its helper functions, which this repository's own CLAUDE.md describes in more forensic detail than any other single file, backed by 122 dedicated tests (`tests/test_job_lifecycle.py`) plus 40 more in `tests/test_maintenance.py`, 38 in `test_r3_proposed.py`, and 30 in `test_r4_gaps.py` — all specifically hardening this exact lifecycle/reaper mechanism against prior rounds of mutation testing. Two independent design-stage reviews already found genuine correctness gaps in this design before any code was written (not stylistic notes) — that track record is itself evidence this area is unusually easy to get subtly wrong, which is the definition of elevated risk for the *implementation* phase, not just the design phase.

## change_classes
- `data-model-migration` — two new nullable-turned-not-null columns (`Job.reaper_resumes`, `Job.claim_token`), both `server_default="0"`
- `core-lifecycle-mechanism-change` — a new fencing-token check added to five existing compare-and-set call sites inside `job_task`'s own machinery (`_advance`, `heartbeat`, `lock_job`, `_settle_failure`'s two CAS calls, `_heartbeat_loop`'s independent periodic write)
- `internal-api-addition` — new `worker/tasks/maintenance.py` resume branch, `_RESUMABLE_TASKS` allowlist
- `bug-fix` — `generate.py`'s `job.meta` reset for the pre-existing (now newly-common) stale-key leak
- `docs-update` — `CLAUDE.md`, `docs/roadmap.md`

## impacted_repositories
- `reel-maker` (this checkout) — sole impacted repository.

## impacted_services
- **`generation` queue** (`enrich_context`, `generate_guide`, `publish_cut`, `reap_stuck_jobs`) — `enrich`/`generate` become resumable; `publish` explicitly, structurally excluded (allowlist omission + module-level assert per the design's §4).
- **`rendering` queue** (`render_cut`) — becomes resumable.
- **Celery beat** (`reap_stuck_jobs`, every 60s) — gains the new resume branch inside its existing `_reap_one` per-candidate loop; no change to its own scheduling, routing, or the `pending`-stale/`done`-orphan candidate paths.
- All four job-backed tasks' shared `job_task` decorator (`worker/tasks/common.py`) — every task in the system passes through the modified claim/heartbeat/fail machinery, whether or not that specific job type is resumable. This is the widest blast radius in this pipeline's three fixes so far: even `publish_cut`, which never resumes, still executes through the same modified `_advance`/`heartbeat`/`lock_job`/`_settle_failure` functions, so a regression here could affect every job type, not just the three intentionally resumable ones.

## impacted_contracts
- **Internal function contracts — additive parameters, not breaking, but touching the shared spine every task body relies on:**
  - `worker/tasks/common.py::_advance()` (`:88`) gains optional `token: int | None = None`.
  - `heartbeat()` (`:102`), `lock_job()` (`:119`) — both gain an internally-supplied token (no signature change visible to task-body callers, which is the design's own explicit goal).
  - `_heartbeat_loop()` (`:201`) gains a required `claim_token: int` positional parameter — this DOES change its signature; the one caller (`job_task`'s own `run()`, inside `worker/tasks/common.py` itself) must be updated in the same commit.
  - `_fail_job()` (`:219`) gains optional `token: int | None = None`.
  - `_settle_failure()` (`:377`) gains optional `token: int | None = None`.
- **No change** to `job_task()`'s own public decorator signature (`:507`) or to any task body's signature (`enrich_context`, `generate_guide`, `render_cut`, `publish_cut` all keep `(self, db, job, ctx)`), confirmed by reading each.
- **No HTTP-facing contract change.**

## impacted_data
- **Two new columns on `jobs`:** `reaper_resumes` and `claim_token`, both `Integer, nullable=False, server_default="0"`, migration `0013` (confirmed next-free number — `migrations/versions/` currently tops out at `0012_rendered_pins_fingerprint.py`).
- **Precedent for the single-step `nullable=False, server_default=...` pattern against a live, populated table already exists in this codebase**: `migrations/versions/0002_improvements.py` added `cut_assets.beat_index`/`order_in_beat` (`server_default="0"`) and `assets.safe_to_publish` (`server_default="false"`) the same way, with no separate backfill migration — the design's own §5 cites this precedent and it checks out.
- **No change** to `Reel`, `Cut`, `CutAsset`, `Asset`, `Credential`, or `StageEvent` schemas.
- **No new table.**

## impacted_dependencies
- **None.** No new third-party package. SQLAlchemy 2.0.52 (confirmed installed) already supports the `.returning()` pattern the design offers as an alternative to `db.refresh(job)` for reading back the post-increment `claim_token` — either approach is implementable with what's already a dependency.

## impacted_owners
Same as this pipeline's prior two reports: no formal team/ownership model, single-operator tool. Sole owner is the repository's maintainer for every surface touched.

## required_tests
Directly named by the design's revised rollout plan (§9), cross-checked against real file locations and current test counts:
1. `tests/test_job_lifecycle.py` (currently **122 tests**, confirmed via `pytest --collect-only`, not just `grep -c`, which undercounts due to multi-line `def` signatures) — new cases: claim bumps `claim_token`; the core "resumed while still alive" regression (stale token T1 vs. fresh T2, `heartbeat()`/`lock_job()`/done-stamp all correctly raise `JobLost`/discard); **both round-two gaps, named explicitly as required, not-yet-existing tests** — a zombie's `_settle_failure` retry-reset and fail-CAS calls must no-op (0 rows) against a superseded token, and `_heartbeat_loop`'s own write must not advance `heartbeat_at` once superseded; every one of the existing 122 tests must still pass unmodified with the token machinery added (proving it's invisible to the non-resume common case).
2. `tests/test_maintenance.py` (currently **23 tests**) — per-job-type differentiated resume budgets (`enrich`/`render` at 2, `generate` at 1); `publish` never resumes regardless of `reaper_resumes`'s value (the one hard-safety negative case); `pending`-stale/`done`-orphan candidates unaffected (existing tests must pass unmodified).
3. `tests/test_generate_task.py` (currently **11 tests**, already home to two dedicated `job.meta`-across-retry regression tests per its own file history) — new case: a killed attempt's committed `structured_fallback`/`structured_score` must not leak into a subsequent clean structured-path success's final `job.meta` (confirmed exact write site: `worker/tasks/generate.py:408`).
4. Migration round-trip (`upgrade head` / `downgrade -1` / `upgrade head`) against real Postgres, per this repository's established practice for every prior migration.

## operational_impacts
- **Render/generate/enrich latency**: negligible per-claim overhead — one extra `db.refresh()` (or one `.returning()` round trip) at claim time, one extra `WHERE` predicate on each of five already-existing, already-primary-key-indexed queries.
- **Worker restart/deploy**: none beyond the standard "workers don't auto-reload, `pkill` + restart after code changes" convention already documented in `CLAUDE.md` — this applies with extra weight here, since a worker running the *old* `job_task` code (no token awareness) alongside a worker running the *new* code during a rolling deploy would have an old-code worker's CAS calls never pass a token at all, which is safe (a `None` token is a no-op filter, matching pre-migration behavior) but means the fencing protection is only as strong as the *newest* deployed worker until the rollout completes — worth a one-line deployment note, not a blocker, since the migration itself (schema) and the code (behavior) can land independently and safely in either order given the `server_default`.
- **Reaper cadence**: unchanged (still every 60s via Celery beat); the new resume branch adds one extra conditional CAS attempt per `running`-stale, resumable-type candidate per pass — bounded by however many jobs are actually stale in a given pass, not a new periodic cost.
- **No feature flag** — single-PR rollout per the design's own §9, consistent with every other roadmap item shipped through this pipeline, but see `review_triggers` below for why this one specifically should not skip a dedicated pre-merge review pass the way a smaller fix might.

## review_triggers
1. **CRITICAL — re-verify both round-two fencing gaps are actually closed in the merged diff, not just described in the design.** A reviewer must independently confirm: (a) `_settle_failure`'s retry-reset CAS and its `_fail_job` call both receive and forward `token=`; (b) `_heartbeat_loop` receives `claim_token` as a parameter and its own `_advance()` call passes it; (c) both of the design's §9-mandated new regression tests exist and are mutation-tested (temporarily strip the token check, confirm the specific new test fails, restore). Do not accept "the design says so" as evidence — this is exactly the class of thing the first cut of this design *also* said was handled and wasn't.
2. **The full existing 122+23+11 = 156 tests across `test_job_lifecycle.py`/`test_maintenance.py`/`test_generate_task.py` must pass unmodified** (not "mostly pass," not "adjusted") — any test in this set that needed a change to keep passing is itself a signal the token/resume addition touched behavior it shouldn't have.
3. **Rolling-deploy ordering** (operational_impacts above) — confirm the design's implicit assumption (a `None`-token CAS is always safe, matching pre-migration behavior) actually holds for every one of the five modified call sites, not just the ones a reviewer happens to trace first.
4. **`generate`'s differentiated resume budget (1, not 2)** — confirm this specific number actually landed in `_RESUMABLE_TASKS` and wasn't simplified back to a single shared constant during implementation, which would silently reopen the cost-multiplication risk the design's §7 exists to bound.
5. **The `job.meta` reset in `generate.py`** — confirm it clears exactly `{"structured_fallback", "structured_score", "path"}` (not a broader or narrower set that could either miss the leak or destroy unrelated, legitimately-persisted meta keys like `context_score`/`performance_note_ids`).

## unknowns
None beyond what the design's own two revision rounds already surfaced and closed. No external API surface exists in this design (unlike the SRT caption feature), and no rollout-gap trade-off is being knowingly accepted (unlike the pins-staleness fix's `None`-means-legacy decision) — the `server_default="0"` migration pattern closes that class of question outright for both new columns.

## evidence_refs
- `docs/specs/2026-09-reaper-resume-killed-jobs-system-design.md` (full document, both revision rounds)
- `worker/tasks/common.py:88` (`_advance`), `:102` (`heartbeat`), `:119` (`lock_job`), `:201` (`_heartbeat_loop`), `:219` (`_fail_job`), `:377` (`_settle_failure`), `:424` (`_fail_job_keep_owner`, confirmed unchanged), `:507` (`job_task`)
- `worker/tasks/maintenance.py:37` (`STALE_MINUTES`), `:44` (`PENDING_STALE_MINUTES`), `:104` (`reap_stuck_jobs`), `:135` (`_reap_one`), `:158` (`_revert_owner`)
- `api/models.py:181-196` (`Job` class, confirmed current columns — `attempts`/`progress` are `default=0` only, no `server_default`, matching the design's claimed contrast)
- `migrations/versions/` — confirms `0012_rendered_pins_fingerprint.py` is current head, `0013` free; `0002_improvements.py` confirmed as the cited precedent for the `server_default` pattern
- `tests/test_job_lifecycle.py` — 122 tests confirmed via `pytest --collect-only -q` (not `grep -c`, which undercounts)
- `tests/test_maintenance.py` — 23 tests; `tests/test_generate_task.py` — 11 tests, confirmed via `grep -c "def test_"`
- `worker/tasks/generate.py:408` (exact `job.meta["structured_fallback"]`/`["structured_score"]` write site)
- SQLAlchemy version confirmed 2.0.52 via `python -c "import sqlalchemy; print(sqlalchemy.__version__)"`
