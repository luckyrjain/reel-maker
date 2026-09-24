# Roadmap

Status as of 2026-08-02.

**Product-level gap analysis and forward roadmap (Phase 4a onward):**
[`docs/product-gap-analysis-and-roadmap-2026-08.md`](product-gap-analysis-and-roadmap-2026-08.md).
This file below is the build log (what shipped, phase by phase); that one is the
"what's missing and what to build next" strategic view.

---

## Phase status

| Phase | Status | Description |
|---|---|---|
| 0 | ✅ Done | Foundations |
| 1 | ✅ Done | Generation pipeline |
| 2 | ✅ Done | Render pipeline |
| 3 | ✅ Done | Review / edit loop |
| 3.5 | ✅ Done | Hardening (reliability, observability, security) |
| 3.6 | ✅ Done | Pre-generation context evaluation & enrichment |
| 3.7 | ✅ Done | Generation quality fixes (context drift, audio gaps, prompt fences) |
| 3.8 | ✅ Done | Repo hygiene, render-path fixes, retry semantics, task-module split |
| 3.9 | ✅ Done | Job lifecycle consolidation (`job_task`) and reliability hardening |
| 4a | ✅ Done | Operator visibility (cost, latency, quality, budget cap) |
| 4b | ✅ Done | Publishing (OAuth, safe_to_publish gate, YouTube + Instagram uploaders, TikTok platform) |
| 5 | ✅ Done | Analytics and polish |
| 6 | 🔲 Partial | Creative range — hook/thumbnail variant generation done; per-reel TTS voice, non-football fixtures, brand customization not started |

---

## Completed

### Phase 0 — Foundations
- Async job pipeline: `POST /api/reels` → Celery task → HTMX polling
- DB schema: reels, cuts, jobs, assets, cut_assets, credentials
- State machine: `REEL_TRANSITIONS`, `CUT_TRANSITIONS`, `transition()` guard
- No-op task for smoke testing the pipeline end-to-end

### Phase 1 — Generation pipeline
- Two-path guide generation: structured-script parser (≥3 ALL-CAPS headers) + standard LLM path
- Structured path: `script_parser.py` extracts beats, `_enrich_with_insight()` adds tactical depth, `_make_conflict_stub()` injects tension
- Standard path: full `MasterGuide` JSON from `build_messages()` + up to 3 retries
- Two-tier quality evaluation: 9-axis rule scorer + LLM semantic judge (5 dimensions)
- Closed-loop eval retry: failure issues fed forward as a second user message; best-of-3 accepted below threshold
- Explicit generation path selection: form dropdown (auto/structured/standard) stored in `job.meta`
- Caption/hashtag/title derived from niche (not hardcoded)

### Phase 2 — Render pipeline
- Pexels stock footage download + DB caching
- Wikipedia player headshots with license metadata (`license_url`, `attribution`, `safe_to_publish`)
- Edge TTS with `synth_to_budget()` — adjusts speaking rate ±25% to hit duration target
- MoviePy 2.x compositor: Ken Burns zoom on photos, multi-image beats, font auto-scaling
- FFmpeg drawtext text overlays: Whisper word-level timestamps (with proportional fallback)
- TTS-driven beat duration: `ffprobe` measures actual audio; clips extend to `audio + 0.1 s`
- Atomic final write: FFmpeg → `.tmp.mp4` → `os.replace()` → final path

### Phase 3 — Review / edit loop
- Editable cut card: `visual_direction`, `vo_script`, `on_screen_text` per beat
- Editable caption and hashtags
- `PATCH /api/cuts/{id}` — saves edits, returns fresh card fragment in-place
- Approve button: `POST /api/cuts/{id}/approve` → `approved` status
- Re-render from `in_review`: only changed beats re-resolve assets (deterministic re-render)

### Phase 3.5 — Hardening
**Reliability**
- `task_acks_late=True`, `task_reject_on_worker_lost=True`, `visibility_timeout=7200 s`
- Idempotency guards in both tasks (`done|running` → return)
- Heartbeat at every milestone; `reap_stuck_jobs` Celery beat (60 s) fails stale jobs (shared `heartbeat()` since Phase 3.8)

**Asset pinning**
- `resolve_or_reuse()`: fingerprints `visual_direction` per beat; reuses pinned `CutAsset` rows without API call when unchanged; unique constraint `(cut_id, beat_index, order_in_beat)` enforces one binding per slot
- `CutAsset.start_s`/`end_s` updated after TTS measurement for accurate timecodes

**Observability**
- `StageEvent` table: one row per pipeline stage (`enrich`, `generate`, `judge`, `composite`)
- `record_stage()` context manager: latency, ok/fail, detail JSON, provider, model

**Security**
- `api/crypto.py`: Fernet `Encrypted` TypeDecorator on `Credential.token_blob`
- Graceful no-op when `CREDENTIALS_KEY` unset (dev environments)

**Quality fixes** (caught by automated code review)
- Removed `were` → `we're` regex (corrupted past-tense VO)
- Fixed `if actual:` falsy-zero bug in TTS duration measurement
- Fixed hardcoded Argentina caption/hashtags/title
- `on_screen_text` cap raised to 5 in both `clean_guide()` and PATCH endpoint
- Stale `CutAsset` rows cleared before re-render; timecodes now use TTS-accurate durations
- notxt.mp4 temp file cleaned in `finally`; FFmpeg stderr surfaced in error message
- Atomic MP4 write via `os.replace()`
- `_FONT_CANDIDATES` aligned with `_get_font` (Linux TTF path)

**Tests** — 91 tests across 7 files:
- `test_evaluator.py` (16), `test_script_parser.py` (12), `test_state.py` (11), `test_enrichment.py` (9), `test_audio_text_sync.py` (~18), `test_context_enricher.py` (13), `test_enrich_context_task.py` (6)

**Evaluator upgraded to 17 axes** — added Conversational Tone, Hook-CTA Throughline, Per-Beat Specificity, Repetition; fixed Insight Density to per-beat distribution; CTA quality now weighted (prediction/opinion > passive follow); hook quality checks real VO signals instead of beat type

### Phase 3.6 — Pre-generation context evaluation & enrichment

**Context evaluator** — `engine/generation/context_enricher.py`:
- `evaluate_context(context)` — 5-axis rule scorer (length, specificity, stakes/tension, narrative arc, hook potential); 0–100; fast, no LLM call
- `llm_enrich(context, niche, llm)` — single LLM call to add specificity, stakes, and a hook angle; returns enriched string or None on failure
- Threshold: score < 60 triggers enrichment; original always preserved in `reel.context`

**`enrich_context` Celery task** — runs before `generate_guide`:
- Evaluates context quality; stores `context_score` + `context_issues` in `job.meta`
- If score < 60: enriches via LLM; stores result in `reel.enriched_context`
- Creates and enqueues a new `generate_guide` job; transitions reel `enriching → generating`
- Non-fatal: LLM enrichment failure is logged and skipped; `generate_guide` always runs

**State machine changes**: `draft → enriching → generating`

**UI changes**: reel-level polling via `GET /api/reels/{id}/active-job-fragment` tracks enrich → generate transition seamlessly; `pipeline_status.html` shows "context prep" badge → "standard LLM" badge as jobs chain

**`generate_guide` change**: `effective_context = reel.enriched_context or reel.context` — all generation, scoring, and evaluation use the enriched context when available

**Evaluator Axis 9 (Audio Delivery) hardened**:
- `MAX_WPS_HOOK = 3.0` — separate tighter WPS cap for hook beats (vs 4.0 for body/CTA)
- Hook pacing violations cost 4 pts each; body violations cost 2 pts each
- Axis 9 max deduction raised from 5 to 10 pts — a rushed hook now fails the quality gate

**Beat duration editor** — `duration_s` field in the cut editor UI (number input, step 0.5, range 1–30); `PATCH /api/cuts/{id}` parses `beat_{i}_duration_s` and saves it to the guide

### Phase 3.7 — Generation quality fixes

**Structured script enrichment guard** (`worker/tasks/enrich_context.py`):
- Structured-script detection skips `llm_enrich()` even when score < 60 (since Phase 3.8 this is `script_parser.is_structured()`, shared with the parser)
- Fixes: enricher was transforming squad-review scripts into World Cup Final narratives by "adding stakes"
- `job.meta["enrich_skipped"]` records reason (`"structured_script"` or `"score_above_threshold"`)

**Topic fence on insight + conflict prompts** (`worker/tasks/generate.py`):
- `_enrich_batch()` and `_make_conflict_stub()` now include: "Do not introduce matches, tournaments, scorelines, or players not mentioned in the beat/context"
- Fixes: enrichment LLM was inserting off-topic events into structured-script beats

**Audio crossfade** (`engine/render/compositor.py`):
- 120ms audio fade in/out (`AudioFadeIn`/`AudioFadeOut` via `.with_effects()`) applied per beat clip before `with_start(t)`
- Fixes: hard-cut audio gaps at beat boundaries made narration feel disconnected
- Also fixed pre-existing bug: `with_start(t)` was called twice on the long-clip branch

**Visual direction anchoring** (`engine/generation/prompt.py`):
- `build_visuals_messages()` system message now anchors the LLM to per-beat VO content only: "Do not use the global context or topic to infer additional visual content beyond what the VO explicitly mentions"
- Fixes: visual directions drifting to match enriched/global context instead of the actual beat

**Tests**: 97 total across 7 files (+6 new tests: 3 enrichment guard, 2 topic fence, 1 visual anchoring)

---

### Phase 3.8 — Repo hygiene, render fixes, retry semantics

Spec: `docs/superpowers/specs/2026-08-02-retry-and-task-cleanup-design.md`

**Repo hygiene**
- Added `.gitignore`; tracked the ~58 source files that had never been committed
- `.env`, `.venv/` (355 MB) and `data/` (1.2 GB) were untracked but unignored — one `git add -A` from committing secrets

**Render path**
- `TTS_PROVIDER` default `chatterbox` → `edge`. The old default matched no implementation, fell through to `SilentProvider`, and — because `render_cut` overwrites each beat's duration with the measured audio length — collapsed every reel to ~1 s per beat, silent, while still reporting `done`
- Wikipedia and both HuggingFace sourcers now write atomically; a killed download used to poison the cache permanently
- Whisper model cached per process (was reloaded once per beat); caption timing falls back to proportional when a transcript has fewer words than the beat has text lines

**Reliability**
- `max_retries=2` made real via `worker/tasks/common.py::should_retry()` — transient failures only, 30 s/60 s backoff. The retry branch resets `job.status` to `pending` first, or redelivery would hit the task's own idempotency guard and no-op
- Reaper now also fails `pending` jobs never picked up (keyed on `updated_at`, so retry backoff survives), and rolls back reels stuck in `enriching`
- Missing reel/cut rows raise a named error instead of `AttributeError`

**Cleanup**
- `generate.py` 652 → 452 lines: `visual_fallback.py` and `beat_enrichment.py` extracted; `heartbeat()` shared instead of three copies
- Deleted dead code: `noop` task, `GET /api/jobs/{id}/fragment`, `job_status.html`, `resolve_beat_asset()`, unused schemas
- Tests 97 → 144 across 14 files

**Live end-to-end run performed 2026-08-02** (Postgres, Redis, both workers, real
Ollama-cloud generation, real Pexels/Wikipedia footage, real Edge TTS audio, real ffmpeg
render) — found and fixed one severe bug the mocked suite could not catch:

- **Every rendered video had zero audio, silently, since the fade-in/out feature shipped.**
  `composite_cut()` called `AudioFileClip.audio_fadein()`/`.audio_fadeout()` — methods that
  do not exist in MoviePy 2.x (fades are effects: `.with_effects([AudioFadeIn(d), AudioFadeOut(d)])`).
  TTS synthesis succeeded, the files were real, but loading them into the composite raised
  `AttributeError`, caught by a bare `except Exception: pass`, so every beat rendered silent
  while the job still reported `done`. Fixed; the handler now logs via `_log.exception()`;
  added `tests/test_compositor.py` — the first real (non-mocked) MoviePy/ffmpeg integration
  test in the suite, which fails against the old code and passes against the fix.

This is the exact class of gap the mocked test suite cannot see. Everything else — enrich,
structured/standard generation path selection, the transient-retry mechanism (fired for
real against a genuine Ollama timeout and recovered correctly), Wikipedia photo sourcing,
Edge TTS synthesis, and the final MP4/thumbnail — worked as designed.

---

### Phase 3.9 — Job lifecycle consolidation and reliability hardening

The guard / stamp / retry / failure stanza had been copied into four task modules (and drifted:
`heartbeat()` before, then the retry fix landing in three separate commits). It now lives in
`worker/tasks/common.py::job_task`, with the owner-state table `api/state.py::JOB_IN_FLIGHT` shared
by the tasks and the reaper. The concurrency-sensitive parts (the atomic claim, the fenced done-stamp,
lock ordering against the reaper, idle-in-transaction across long calls) were verified against real
PostgreSQL 16, not just SQLite. Behaviour changes an operator should know about:

- **Only `pending` jobs run.** The claim is an atomic `UPDATE … WHERE status='pending'`; `failed` is
  terminal (an operator retry creates a new Job). The done-stamp and `heartbeat()` are fenced the same
  way, so a worker the reaper gave up on cannot commit or keep writing.
- **The reaper now actually runs.** `reap_stuck_jobs` had no `task_routes` entry, so beat sent it to the
  default queue that no documented worker consumes. It is now routed to `generation`, does a
  per-job compare-and-set, and rolls back only the owner state the job's type owns. The first run after
  deploy will fail any historic stale rows.
- **A heartbeat thread** keeps `heartbeat_at` fresh through long LLM / ffmpeg / upload calls, and each
  task has a `max_runtime_s` enforced as a Celery soft/hard time limit.
- **Publishing never retries automatically** (`max_retries=0`, no release on shutdown). The post id is
  committed the moment the upload succeeds, and a re-run with an id set finalizes without uploading.
  A cut that already has a post id cannot be re-rendered. A timeout *inside* the upload can still lead
  to a manual double post: the failed-cut card tells the operator to check the platform first.
- **Routers fail fast** when `.delay()` raises (503, job failed, cut/reel rolled back) instead of
  leaving the owner in flight until the reaper's 4 h pending threshold.
- `OperationalError`/`InterfaceError` are transient; `pool_pre_ping` and `hide_parameters` are on;
  `job.error` is sanitised (never empty, database row detail and bound parameters redacted, a soft
  time-limit failure says so in plain language); task sessions use `expire_on_commit=False`.
- **A worker shutdown (or a hard time limit) fails the job at once**, freeing its owner immediately,
  rather than handing it back to `pending` — Celery has already acked or dropped the message by then,
  so a `pending` job would otherwise wait for the reaper's threshold with no message coming back for it.
- **`approve_cut` and `update_cut` also lock the cut row**, matching the trigger routes, so an approve
  cannot race a render past its own status guard. `update_cut` reads the request body before taking
  that lock — a slow client otherwise holds a pool connection for as long as the body trickles in.
- **Terminal failure recorders survive a dead connection.** `_fail_interrupted`, `_fail_rejected_retry`,
  and `fail_unenqueued` retry once on a brand-new session (`_finalize_or_reconnect`) if their own
  `db.rollback()`/write/commit raises `OperationalError`/`InterfaceError` — plausible exactly when one
  of these is running, since a server-side kill or the idle-in-transaction timeout can be the reason.
- **A refused retry (`Reject`) fails the job whether or not this run ever claimed it** — an unclaimed
  job whose retry message the broker refused was previously left `pending` until the reaper's threshold.
- **`after_commit` raising `SystemExit`/`KeyboardInterrupt`** (not just `Exception`) still runs the
  `after_commit_failed` cleanup hook; if the hook itself raises, the failure stamp it already wrote is
  still committed rather than silently rolled back with the job left looking `done` forever. The hook
  runs inside its own `db.begin_nested()` SAVEPOINT (managed explicitly — `.rollback()`/`.commit()`
  called directly, not `with db.begin_nested():`), so a raise partway through a multi-write hook
  (`_abandon_generate`'s real shape: fail the orphaned follow-up Job, then roll the reel back) undoes
  only the hook's own writes — not the failure stamp, and not a half-done cleanup either. A
  SystemExit/KeyboardInterrupt raised BY THE HOOK ITSELF (not by `after_commit`) is deliberately not
  swallowed, but does commit the fail-stamp before re-raising via `_commit_stamp_and_reraise` —
  without that, the exception unwinding straight to `job_task`'s `finally: db.close()` would roll it
  back too. That commit is itself guarded: if it fails, the failure is logged and the original
  BaseException still propagates rather than being replaced by the commit error. If the SAVEPOINT
  rollback itself fails, `_recover_from_hook_failure` (`worker/tasks/common.py`) falls back to a full
  `db.rollback()` (discards the hook's still-pending writes, which the failed SAVEPOINT rollback never
  actually did) and redoes the fail-stamp CAS on the now-clean transaction — and that redo is itself
  guarded too, so a third failure in the same sequence can't replace the shutdown signal that survived
  the first two. No log line in this whole path claims the fail-stamp is durably "recorded": neither
  caller commits `db` until after this function returns, so every log line says "staged (not yet
  committed)" or, when even that can't be confirmed, says so plainly and points at the database.
  - **One narrow gap remains, not worth a code change today**: if the hook raises BaseException *and*
    the guarded recovery commit *also* fails (at any of the several points that guard applies), the Job
    row is left at `status=done` with no error recorded, and the reaper's sweep only scans
    `running`/`pending` rows — that Job is never revisited. This isn't really an independent unlucky
    coincidence: the same dying connection that fails the SAVEPOINT rollback plausibly fails the very
    next commit too, so it's one failure mode with several consecutive symptoms, not a rare one.
    `enrich_context`'s hook (`_abandon_generate`) happens to self-heal the reel anyway, because its own
    orphaned follow-up Job stays `pending` and gets reaped after `PENDING_STALE_MINUTES`; that's
    incidental to `_abandon_generate`'s specific shape, not a guarantee `_stamp_failed_and_run_cleanup`
    makes for every hook. A future hook with no such side effect would leave its owner stuck in its
    in-flight status with no automatic recovery in this specific double-fault. **Mitigated**:
    `reap_stuck_jobs` (`worker/tasks/maintenance.py::_done_orphan_candidates`) also sweeps `done` jobs,
    `error IS NULL`, whose owner is still sitting in the state `JOB_IN_FLIGHT` maps to that job type,
    past `DONE_ORPHAN_STALE_MINUTES` (15) — one query per job type, joined to the owner table so a
    genuinely successful `done` job (indistinguishable from a stuck one by the Job row's own columns
    alone) never matches. Covered by `tests/test_maintenance.py`.
  - **Detecting it**: the only trace is a handful of log lines from logger `worker.tasks.common`
    (`SAVEPOINT rollback itself failed`, `could not roll back the poisoned transaction`, `could not
    redo the failure stamp`, `could NOT confirm`, `could not load job ... for the cleanup hook`, `could
    not record failure of job`) — there is no alerting on any of them (this repo has none configured
    for anything), so this is moot until some alerting exists. A Job whose `status` is `done`, `error`
    is `None`, and whose owner (reel/cut) is still in the in-flight state `JOB_IN_FLIGHT` maps to that
    job type is the on-disk signature; `reap_stuck_jobs` now queries for that combination every 60 s.

  <details>
  <summary>Investigation history (rounds 10-14) — why this code looks the way it does</summary>

  - **Round 10**: introduced the SAVEPOINT so a multi-write hook rolls back atomically on its own raise.
  - **Round 11**: found that a failed SAVEPOINT rollback could mask the hook's own shutdown signal
    (`SessionTransaction.rollback()` re-raises a failed DBAPI-level rollback rather than swallowing it,
    replacing the propagating `SystemExit`/`KeyboardInterrupt` with an ordinary `Exception`). First fix
    inferred this shape from `exc.__context__` after the fact.
  - **Round 12**: found the `__context__` inference had a false-positive of its own — `__context__`
    reflects whatever exception is ambiently "being handled" anywhere up the call stack (e.g. when this
    function runs with `on_shutdown=True`, already inside `job_task`'s own outer shutdown handling), not
    necessarily anything to do with the hook's own SAVEPOINT, so an ordinary unrelated hook bug could
    misclassify as a recovered masked shutdown. Also found the round-11 fix's recovery path could commit
    the hook's still-pending writes (never actually discarded by the failed SAVEPOINT rollback) alongside
    the fail-stamp, breaking the "only the hook's own writes roll back" guarantee. Fixed both by managing
    the SAVEPOINT explicitly instead of inferring from `__context__`, and by doing a full `db.rollback()`
    + fail-stamp CAS redo when the SAVEPOINT-scoped rollback fails. Independently re-verified against
    real PostgreSQL 16 for both fixes.
  - **Round 13**: found the redo CAS itself could fail (a third failure in the same sequence), still
    capable of masking the shutdown signal if left unguarded; fixed. Found the SAVEPOINT-rollback-failure
    branch's log line claimed more than was confirmed when the recovery *also* failed; fixed (tracked
    explicitly via a `confirmed` flag). Found `db.get(models.Job, job_id)` failures were misattributed to
    "the hook raised" when the hook never actually ran; moved the read out of the hook's own try/except.
    After four consecutive rounds each finding a real bug in the same ~20-line recovery ladder, extracted
    it into `_recover_from_hook_failure(db, job_id, message, nested) -> bool`, following the same idiom
    already used twice in this file (`_commit_or_log`, `_commit_stamp_and_reraise`) — same behavior, but
    the four recovery outcomes are now independently unit-tested instead of only reachable by also
    driving the outer hook-invocation and shutdown-classification logic.
  - **Round 14**: found the wording fix from round 13 (the SAVEPOINT-rollback-failure branch saying
    "failure stamp staged, not yet committed" instead of overclaiming "still recorded") had only been
    applied to that one rare branch — the much more common "recovery succeeded" branch still overclaimed
    "still recorded" for the exact same reason (neither caller commits until after this function
    returns). Reworded both branches consistently. Separately, `_recover_from_hook_failure`'s redo step
    caught exceptions from `_fail_job_keep_owner` but ignored its boolean return — `False` means the CAS
    (`WHERE status='done'`) didn't match (a sibling or the reaper moved the job on in the gap since the
    full rollback), not an exception, so a lost CAS silently fell through to `return True`, contradicting
    the function's own documented contract ("confirmed durable"). Fixed to check the return value too.
    Independently verified against real PostgreSQL 16 (`pg_terminate_backend`, a real server-side trigger
    to force the redo's `UPDATE` to fail, a second connection to check MVCC visibility of the staged
    write) — confirmed all four original recovery outcomes hold, confirmed the "staged" wording is
    accurate (Postgres has no working `READ UNCOMMITTED`; the write is genuinely invisible to any other
    session until commit, and genuinely can vanish if that commit then fails), and surfaced one adjacent,
    narrower, pre-existing gap not introduced by this ladder: `_fail_job_keep_owner`'s own internal
    `db.get()` (a read-only ORM identity-map refresh, not the CAS itself, and not `_recover_from_hook_failure`'s call
    to it) is unguarded — a connection death exactly there propagates raw into `job_task`'s generic
    catch-all with none of this section's specific diagnostics, even though it's the identical
    underlying race one statement earlier. Not fixed here (out of scope for this ladder, and
    `_fail_job_keep_owner` is used by every job-failure path in this file, not just this one); noted for
    a future pass.

  </details>
- `_generate_caption_hashtags`'s fallback path no longer swallows `SoftTimeLimitExceeded` — a timeout
  there now fails the task visibly instead of completing with a template caption.

## Phase 4a — Operator visibility (done)

Shipped ahead of the original Phase 5a plan below, once the product-gap-analysis
review (2026-08-02) flagged "operator can't see or find their own work" as the
single most-felt daily gap and the prerequisite for everything downstream of it.

- Reel list page (`GET /api/reels`, paginated) and a per-reel pipeline panel
  (`GET /api/reels/{id}`) — cost, latency, quality score, per-stage breakdown,
  sourced from `StageEvent`
- `StageEvent.cost_usd`/`tokens_in`/`tokens_out` implemented for every LLM call
  site (`engine/generation/pricing.py::llm_cost_usd()`, NVIDIA-only — rates are
  operator-configured via `NVIDIA_PRICE_PER_1M_*`, default 0 rather than a
  guessed number). Several previously-uninstrumented call sites (conflict-stub
  generation, caption/hashtag generation, structured-path visuals) got their
  own `record_stage()` wrap as part of this, not just the ones that already had one.
- Pre-generation cost/time estimate on the create-reel form
  (`engine/generation/estimate.py`), sourced from this operator's own
  historical `StageEvent` averages per generation path — not a guessed
  per-token figure. Excludes reels where the structured path fell through to
  standard from the "standard" bucket, since those pay for both attempts and
  would otherwise inflate the estimate.
- `Settings.max_paid_llm_calls_per_reel` — a hard ceiling on NVIDIA-provider
  `StageEvent` count per reel, checked at `generate_guide` task entry and
  before each standard-path retry attempt, so a stuck Celery retry loop can't
  run up an unbounded bill.

Not done: HuggingFace/asset-generation cost tracking (only LLM calls are
instrumented); per-stage latency breakdown exists but nothing calls out which
stage dominates beyond the raw table.

---

## Phase 4b — Publishing (done)

Built on top of Phase 4a's cost/observability plumbing. Deviates from the
original plan below in a few ways, noted inline.

**OAuth + credentials** (`api/oauth.py`, `api/routers/credentials.py`,
`ui/templates/credentials.html`)
- Generic OAuth2 authorization-code flow — connect/disconnect UI at
  `/api/credentials`, `GET /{provider}/authorize` → `GET /{provider}/callback`.
  Endpoint paths differ from the original `/auth/youtube` sketch.
- Instagram publishing rides on a Facebook Page's access token, not the
  user's own token — the callback does an extra long-lived-token exchange,
  then discovers the linked Instagram Business Account via the user's
  Facebook Pages (`InstagramOAuth.discover_account()`). Not anticipated in
  the original plan.
- Migration 0004 adds `Credential.provider_account_id` (the discovered IG
  Business Account ID) and `refresh_token_blob`.

**Publish task** (`worker/tasks/publish.py`, `api/routers/cuts.py::trigger_publish`)
- `publish_cut(job_id)` follows the same idempotency-guard/heartbeat/transient-retry
  lifecycle (`job_task`) as `generate_guide`/`render_cut`, but with `max_retries=0` and no
  release-on-shutdown (an accepted upload must never be repeated). Gates on `assert_safe_to_publish()`
  (`engine/publish/gate.py`) before ever calling a platform API — the first
  place `Asset.safe_to_publish` is actually enforced, not just computed.
- State transitions: `approved|scheduled → publishing → published` or `failed`,
  matching the original plan. `CUT_TRANSITIONS["failed"]` was extended to also
  allow `→ approved` (not just `→ draft`) since a failed *publish* shouldn't
  force a full re-render — the operator's retry action (`/render` vs
  `/publish`) decides which path is taken.
- `reap_stuck_jobs` now also rolls back cuts stuck in `"publishing"`, not just
  `"rendering"` — a killed publish worker would otherwise leave a cut stuck forever.

**Uploaders** (`engine/publish/`)
- `youtube.py` — resumable-upload flow (init POST + one PUT, reels are small
  enough that true multi-chunk resuming isn't needed); refreshes an expired
  access token via the stored refresh token first.
- `instagram.py` — container-create → poll-until-FINISHED → publish flow via
  the Graph API. Requires `PUBLIC_BASE_URL` to be a real public HTTPS
  address — Instagram fetches the video itself from
  `GET /api/cuts/{id}/video` rather than accepting an upload body. This is a
  real external constraint, not a bug: publishing to Instagram will not work
  behind localhost/NAT alone.
- `tiktok.py` — deliberately raises `NotImplementedError`. TikTok's Content
  Posting API requires a separate audited app review beyond self-serve OAuth;
  shipping a guessed integration nobody could verify against a real audited
  app seemed worse than an honest gap. TikTok is still selectable as a
  render/review platform (`CutPlatform.tiktok`) — only publishing is gated.

**Not done (at the time):** the attribution-block-in-caption step from the
original 4c plan; a "schedule for later" UI (the `scheduled` cut status and
the publish task both support it, but nothing currently transitions a cut
*into* `scheduled`). The attribution item shipped in Phase 5 — see below.

---

## Phase 5 — Analytics and polish (done)

Deviates from the original plan below in a few ways, noted inline.

### 5a. Cost tracking

Superseded by Phase 4a for LLM costs. HuggingFace asset-generation cost
tracking (the remaining open item there) shipped in this phase:
`engine/render/pricing.py::hf_image_cost_usd()`/`hf_video_cost_usd()` are
config-driven (`HUGGINGFACE_PRICE_PER_IMAGE`/`HUGGINGFACE_PRICE_PER_VIDEO_SECOND`,
both `0.0` until the operator sets a real rate) and feed `"asset_hf_image"`/
`"asset_hf_video"` `StageEvent`s in `asset_sourcer.py`. Cost is charged only
when a call actually generates an asset — a cache hit (`last_call_was_generated
== False`) records the stage with `cost_usd=0` so re-renders don't double-bill.

### 5b. Post-publish metrics — done, schema differs from the original plan

Instead of a separate `metrics` table keyed by `(cut_id, fetched_at)` (which
would let us keep a full time series), metrics live as four columns directly
on `cuts`: `views`, `likes`, `comments`, `metrics_updated_at` (migration
0005) — the latest pull overwrites the previous one. This matches the
"sortable table, not a dashboard" philosophy: the reel list and cut card show
current standing, not a trend line, and a real time-series table can be added
later without disturbing this shape.

- `engine/publish/metrics.py` — `YouTubeMetricsFetcher` (`videos.list?part=statistics`,
  stable/documented) and `InstagramMetricsFetcher` (Graph API
  `/insights?metric=plays,likes,comments` — flagged lower-confidence since
  Meta has renamed Reels Insights metrics before). No TikTok fetcher (no
  publisher either).
- `worker/tasks/metrics.py::pull_publish_metrics()` — Celery beat task, every
  6 h (not the originally sketched "24h+ only" gate — every published cut is
  re-pulled each run). Missing fetcher/credential or a fetch exception skips
  that cut and moves on; never fails the whole run.
- Surfaced on `cut_card.html` (views/likes/comments + "as of" timestamp once
  pulled, a "checked every 6h" hint before the first pull) and on
  `reels_list.html` as a Quality/Views column pair per reel (latest job's
  `quality_score`, max `views` across the reel's cuts). A computed
  quality-vs-engagement correlation (not just the two eyeballed columns)
  shipped later — see Phase 5g below.

### 5c. Music mixing — done, source differs from the original plan

`music_cue` is now mixed into every render that has a match. Sourcing is a
**local library**, not the Pixabay music API sketched originally — Pixabay's
public REST API has never documented a Music search endpoint (only
Images/Video), so integrating against it would have meant guessing at an
API that may not exist. `LocalMusicSource` (`engine/render/asset_sourcer.py`)
keyword-matches a beat's `music_cue` against filenames in
`Settings.music_library_dir`; no `Asset` row (no external fetch to cache).

- `engine/render/compositor.py::_build_ffmpeg_args()` — sidechain-ducked mix
  (`sidechaincompress`) when the beat has VO audio, plain-volume mix
  (`amix ... normalize=0`) when it doesn't; the looped music input
  (`-stream_loop -1`) is always `atrim`'d to the render's total duration and
  `-shortest` is kept as a safety net — an untrimmed infinite loop previously
  produced a "no space left on device" ffmpeg error.
- Structured-path beats now default `music_cue="upbeat energetic"` on the
  hook beat (`worker/tasks/generate.py::_stubs_to_platform_guide()`) — they
  previously never got a `music_cue` at all, so nothing was ever mixed in
  for that path.

### 5d. Word-level caption export — not done

Whisper is wired in for on-screen text timing, but a separate SRT/VTT caption file for the platform uploader is not generated.

**Work:**
- After Whisper transcription in the compositor, write per-beat segments to an SRT file alongside the MP4
- Pass the SRT to YouTube Data API as a caption track on upload

### 5e. ~~Insight enrichment for standard LLM path~~ ✅ Done (Phase 3.6)

`_enrich_standard_path_guide()` runs after `clean_guide()` in the standard path — converts beats to `BeatStub`-like objects, runs `_enrich_with_insight()`, writes enriched VO + recalculated duration + re-derived on_screen_text back.

### 5f. Attribution block in captions — done

`engine/publish/attribution.py::build_attribution_block()` joins `Asset` +
`CutAsset` for the cut, filters to `source == "wikipedia"` assets with
non-empty `attribution`, dedupes, and formats
`"Image credit: {attribution} ({license}); ..."`.
`build_published_caption()` appends it to `cut.caption` (or stands alone if
the caption is empty) without mutating the DB-stored caption — the publish
task passes the composed string through `Publisher.publish(..., caption=)`,
a new required parameter every publisher now takes instead of reading
`cut.caption` directly.

### 5g. Quality↔engagement correlation + performance-informed feedback — done

Closes the last two Phase 5 items from
`docs/product-gap-analysis-and-roadmap-2026-08.md`. Full design:
`docs/specs/2026-09-phase5-quality-engagement-feedback.md`.

**Correlation** (`engine/analytics/correlation.py`, new `GET /api/insights`
page via `api/routers/insights.py`):
- `quality_engagement_correlation(db)` — Pearson *r* between each reel's
  latest `quality_score` and its max-per-cut `views`, refusing to compute
  below `MIN_SAMPLE=5` reels or when either series has zero variance
  (`np.corrcoef` returns NaN on zero variance — guarded explicitly rather
  than let `"nan"` leak into the UI). Reuses a newly-extracted
  `engine/observability.py::latest_quality_scores(jobs)` helper, also now
  used by `api/routers/reels.py::_reel_list_metrics()` — one definition of
  "this reel's quality score," not three near-identical copies.
- The page always shows `r` together with `sample_size` (never `r` alone),
  and a permanent, non-dismissable caveat: quality scores cluster near the
  acceptance threshold by construction (generation retries specifically to
  clear it), which mechanically attenuates any correlation this number can
  detect — a low/near-zero *r* does not mean quality doesn't matter, it
  means this sample can't measure it well. Sampling below-threshold guides
  on purpose to fix that would mean deliberately publishing worse content —
  a product decision, not something this release does.
- `top_bottom_performers(db, k=3)` — top/bottom-3 by views (one combined
  list when `n < 6`, so the two tables never show the same reel), each row
  including the hook beat's `vo_script` from whichever cut drew the most
  views. Read-only: exists purely so an operator has evidence before
  writing a note, never auto-injected into generation.

**Performance-informed feedback** (`PerformanceNote` — `api/models.py`,
migration `0007_performance_notes.py`): operator-written, plain-English
notes ("Hooks phrased as a direct question outperform statement hooks"),
toggleable active/inactive, CRUD via `POST/DELETE /api/insights/notes*`
(hard-delete — cheap, operator-owned text, no undo needed). Every *active*
note is seeded into `worker/tasks/generate.py`'s standard-path
`prior_feedback` from attempt 1, reusing the existing
`build_messages(prior_feedback=)` injection point rather than adding a new
one. **Deliberately not** the literal ask read automatically ("feed high/low
performers back into `prior_feedback`" as auto-injected few-shot examples)
— the spec's §3.1 rejects that specifically: this codebase's enrichment/
conflict prompts already carry an explicit topic-fence principle ("do not
introduce matches, tournaments, scorelines, or players not mentioned in the
beat/context") because unconstrained prior context has previously leaked
into unrelated generations, and a single-operator/single-niche tool would be
overfitting to a handful of reels if it auto-selected "the best one." Human
curation stays the boundary; only the mechanical plumbing is automated.

Two correctness traps were found and fixed before this shipped, both with
dedicated regression tests written against the buggy version first (see
CLAUDE.md's Key conventions for the full detail): the active-notes query
must run unconditionally at the top of `generate_guide` (not inside the
standard-path-only branch, or every structured-path success NameErrors on
the shared `job.meta` write), and the per-attempt `feedback` reassignment
must be additive (`active_notes + [...]`), not a plain replace, once
`feedback` starts non-empty.

**Evaluator axis weight multipliers** (`Settings.evaluator_axis_weight_multipliers`,
default `{}` — a no-op until configured, and the first dict-typed `Settings`
field in this codebase): `score_guide()` gains an optional `axis_multipliers`
parameter and one small correction block immediately before its final
`return`, reusing the `deductions` dict the function already built for its
issue-string breakdown — none of the 23 existing `score -=` sites changed.
A manual lever informed by the correlation data above, not an auto-tuned
weight (see "Explicitly out of scope" in the spec for why automatic fitting
stays out).

---

## Phase 6 — Creative range (partial)

Sourced from `docs/product-gap-analysis-and-roadmap-2026-08.md`'s Phase 6.
Only the first item is built; the rest are untouched.

### 6a. Hook/thumbnail variant generation — done

Two independent, cheap additions — neither regenerates the guide:

- **Thumbnails**: `engine/render/compositor.py::composite_cut()` now returns
  `(duration, thumbnail_candidates)`. `_write_thumbnail_candidates()` samples
  4 frames per render — the original ~0.5s-in frame first (at
  `thumbnail_path` itself, so a caller that only reads `candidates[0]` sees
  the exact pre-existing behavior), plus 3 more at 25%/60%/85% of the reel's
  duration, written as `{stem}_1/_2/_3{suffix}` siblings. No LLM cost.
  `render_cut` stores the full list on `Cut.thumbnail_candidates` and
  defaults `Cut.thumbnail_path` to `candidates[0]`.
- **Hooks**: `engine/generation/hook_variants.py::generate_hook_variants()` —
  one best-effort LLM call (via `get_enrichment_provider()`) after
  `generate_guide` already accepts a guide, asking for 3 alternate lines for
  the hook beat's `vo_script`. Runs once per reel (not once per cut — platform
  guides normally share identical beats), gated behind the same paid-call
  budget check as the rest of generation, and never raises: any failure
  (bad JSON, provider error, budget exhausted) just means `Cut.hook_variants`
  stays `None`. Cost is tracked via the same `StageEvent`/`llm_cost_usd()`
  path as every other LLM call site (`stage="hook_variants"`).
- Both are operator-picked from the `in_review` cut card
  (`POST /cuts/{id}/thumbnail`, `POST /cuts/{id}/hook-variant` in
  `api/routers/cuts.py`), gated to `in_review` the same way the beat-edit
  PATCH endpoint is. A re-render replaces `thumbnail_candidates` wholesale —
  same as `video_path` — so an operator's thumbnail pick from a previous
  render doesn't survive a re-render.

### 6b. Configurable TTS voice per reel — not done

`voice` param already exists on `EdgeTTSProvider`/`get_tts_provider()`; the
create-reel form has no voice selector and nothing threads a per-reel choice
through.

### 6c. Non-football niche evaluator fixtures — not done

`evaluator.py`'s "universal" fallback patterns exist but have no real test
fixtures outside football content — whether they score other niches fairly
is unmeasured.

### 6d. Brand customization — not done

No logo/watermark, no configurable overlay text color, no per-channel presets.

---

## Open issues

| Issue | Severity | Notes |
|---|---|---|
| `safe_to_publish` gate checks the current pins, not the video that will ship | Medium | `resolve_or_reuse()` re-pins and commits per beat as the operator edits and re-renders; a failed render never clears `cut.video_path`. If render N used a non-free asset (blocked) and a later render N+1 re-pins to a safe one but then itself fails, "Retry publish" gates against the safe N+1 pins while `video_path` still points at render N's (unsafe) video — the gate passes and the wrong video ships, with attribution built from the wrong pins too. Fix would clear `video_path` (or check a pins fingerprint) whenever a render starts or re-pins |
| No multi-image collage in one frame | Low | Currently cycles sequentially; side-by-side layout not implemented |
| MoviePy video readers leak until worker recycle | Low | `_build_media_sub_clip` opens `VideoFileClip`s that only `worker_max_tasks_per_child=10` reclaims; marked with a `ponytail:` comment |
| `asset_sourcer` degrades silently to black frames | Medium | Every sourcer swallows its own exceptions and returns `None`, so a Pexels/Wikipedia outage produces a black-frame reel that reports success — and never reaches the retry branch |
| `resolve_or_reuse()` commits the caller's session | Low | Deliberate (no transaction may sit idle across its network calls or the TTS that follows); noted in its docstring. A partial Wikipedia result (one of several names failing) is pinned and reused until `visual_direction` changes |
| `record_stage()` commits the caller's session | Low | Benign today (all call sites sit on a commit boundary) and documented in `observability.py`, but it will bite whoever wraps a half-applied mutation |
| `_escape_drawtext` escapes only `\ : % '` | Low | A newline or exotic character in `on_screen_text` could break the FFmpeg filter chain; not observed in practice |
| Wikipedia licence lookup uses a percent-encoded filename | Low | `_fetch_license()` passes the raw URL segment, so accented/spaced filenames return "unknown" and default to `safe_to_publish=False`. Now live: `assert_safe_to_publish()` blocks these cuts from publishing (Phase 4b), so this under-detection means some legitimately-safe Wikipedia assets get blocked rather than the reverse (a licensing false-negative, not a false-positive) |
| Reaper does not resume killed jobs | Medium | Under prefork, `pkill` / SIGKILL kills the child without unwinding: the job stays `running`, the redelivered message no-ops, and the reaper fails it after 5 minutes. Nothing re-runs it, so an enrich/generate job loses its (paid) work and a `failed` reel has no retry endpoint. A fix would let the reaper re-enqueue idempotent job types (never publish) a bounded number of times |
| Failure reason is not shown after a page refresh | Low | `job.error` is rendered only in the polling fragment of the tab that started the job; the cut card and reel page show a generic "Failed" |
| Cut page does not poll while rendering | Low | `cut_card.html` shows "refresh to update" instead of an auto-refreshing fragment — same limitation for `"publishing"` status (Phase 4b) |
| LLM judge 60% weight can swing combined score | Low | Log per-attempt rule/judge split from `StageEvent`; tune once data accumulates |
| Whisper `base` model is slow on CPU | Low | Switch to `faster-whisper` with `base` model for 3-4× speedup on same hardware |
| Instagram Page token has no refresh path | Low | `InstagramPublisher` doesn't refresh the stored Page token — matches real Facebook Graph API behavior (a Page token derived from a long-lived ~60-day user token is effectively non-expiring under normal use), but if it ever does expire the operator has to reconnect manually rather than being auto-recovered. YouTube's token *is* refreshed automatically (`engine/publish/youtube.py::get_valid_access_token()`, shared by `YouTubePublisher.publish()` and `YouTubeMetricsFetcher.fetch()`) since Google's short-lived access tokens need it every ~1h. |
| "Retry publish" on a `failed` cut can ship a stale pre-edit video | Low | `CUT_TRANSITIONS["failed"]` intentionally allows both `→ draft` and `→ approved` (see Key conventions in CLAUDE.md) so a failed *publish* doesn't force a full re-render. The same ambiguity also covers a failed *re-render*: if the operator edited the guide then re-rendered and that render failed, `cut.video_path` still points at the last successfully rendered (pre-edit) file, and "Retry publish" on the `failed` card will ship it. Partially mitigated today by an explicit warning in `cut_card.html`'s `failed` branch ("if you edited the guide since then... that video won't reflect those edits"); a full fix would need tracking whether `video_path` reflects the current guide (e.g. clearing it, or a `video_is_stale` flag) rather than just warning about it. |
| Standard LLM path caption/hashtags not content-aware | Low | Generated from niche only, not actual beat content; structured path uses `_generate_caption_hashtags()` from actual VO — standard path could do the same |
| `PIXABAY_API_KEY` config field is unused | Low | Pixabay's public REST API has never documented a Music endpoint (only Images/Video) — Phase 5c deliberately built `LocalMusicSource` instead of guessing at an API that may not exist. Field stays unwired by design, not for lack of time. |
| `paid_call_count()` is a lifetime-per-reel counter | Low | No per-job scoping or reset path — not exploitable today since nothing re-triggers a `generate` Job for a reel that already has one, but a future "regenerate guide" flow would need an explicit reset, not just raising `MAX_PAID_LLM_CALLS_PER_REEL` |
| `pull_publish_metrics()` overwrites rather than accumulates a time series | Low | `cuts.views`/`likes`/`comments` hold only the latest pull — see Phase 5b for the "sortable table, not a dashboard" reasoning. A trend line needs a separate metrics-history table later. |
| Instagram Insights metric names may drift | Medium | `InstagramMetricsFetcher` requests `plays,likes,comments` — Meta has renamed Reels Insights metrics before (e.g. `plays` → `video_views` at points), so a future API change could silently return empty/zero metrics rather than erroring loudly |

---

## Dependency notes

| Dep | Status | Notes |
|---|---|---|
| `edge-tts` | Required | Not in pyproject.toml extras; `pip install edge-tts` separately |
| `openai-whisper` | Optional | `pip install -e ".[captions]"`; without it, proportional text timing is used |
| `faster-whisper` | Not installed | Drop-in replacement for `openai-whisper`; same `transcribe_audio()` interface |
| `kokoro` | Optional | Requires Python < 3.13; not compatible with Python 3.14 |
| `cryptography` | Required for publish | Fernet; no-op import check in `api/crypto.py` if not installed |
