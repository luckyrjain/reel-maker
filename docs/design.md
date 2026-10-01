# Design

A reference for the SHAPE of this system's major engineering decisions, and WHY they were
made — pitched between `docs/architecture.md` (modules, diagrams, request flows — read that
first for the module-by-module breakdown) and `CLAUDE.md`'s "Key conventions" section (the
exhaustive, blow-by-blow implementation journal, including every review round that caught a
real bug). This doc is the middle layer: enough to orient a new engineer on *why* the code
looks the way it does, with pointers into the real files and the longer writeups for anyone
who needs the full history.

---

## 1. Async-job architecture

**What:** Every operation slower than a DB write — LLM generation, asset sourcing, TTS,
ffmpeg rendering, platform uploads — runs as a Celery task behind a `Job` row. Nothing slow
happens inside an HTTP request. The browser never receives JSON (except `GET /api/jobs/{id}`);
every route returns a Jinja2 HTML fragment, and HTMX polls and swaps those fragments into the
DOM (`GET /api/reels/{id}/active-job-fragment` every 2s, `GET /api/cuts/{id}/render-status`
every 3s).

**Why:** A single operator kicking off a generation (2–5 min) or a render (minutes, CPU-bound
ffmpeg) cannot sit on an open HTTP connection for that long, and a request-cycle timeout would
just move the failure mode somewhere worse. Modeling every stage as an async `Job` with its own
status gives the UI something durable to poll, gives the reaper (§3) something to recover, and
gives `StageEvent` (§7) something to instrument — all three of which depend on the job boundary
existing in the first place.

**Why HTML fragments, not JSON + a frontend framework:** there is no SPA and no build step by
design. A single-operator tool server-rendering Jinja2 + HTMX is simpler to deploy, debug, and
extend than a decoupled API + JS client, for a UI that is fundamentally "poll a status, show a
form, show a video." See `docs/architecture.md`'s request/response pattern section for the full
flow diagrams.

Pointers: `worker/celery_app.py`, `api/main.py`, `ui/templates/fragments/*.html`.

---

## 2. State-machine discipline

**What:** `REEL_TRANSITIONS` and `CUT_TRANSITIONS` (`api/state.py`) are dicts of
`{current_status: {allowed_next_statuses}}` — the single source of truth for every legal status
change on a `Reel` or `Cut`. `transition(obj, new_status, map)` is the *only* sanctioned way to
change `.status`; it raises `ValueError` on an invalid move. Application code never sets
`.status` directly.

**Why:** A reel/cut moves through many stages (`draft → enriching → generating → guide_ready`,
`draft → rendering → in_review → approved → publishing → published`, plus `failed` branches
reachable from almost every stage) touched by multiple actors — routers, Celery tasks, the
reaper. Without a single enforced transition table, it's easy for two code paths to disagree
about what a given status even means, or for a race to leave an object in a status nothing can
recover it from. Centralizing the table means every valid path is visible in one place, and an
invalid transition fails loudly (a bug) instead of silently corrupting state.

**A specific, deliberate wrinkle:** `Cut` status `"failed"` is ambiguous on purpose — it covers
both a failed render and a failed publish, and `CUT_TRANSITIONS["failed"]` allows both
`"draft"` (retry render) and `"approved"` (retry publish). Which target applies is decided by
which endpoint the operator hits next, not tracked on the `Cut` itself. See CLAUDE.md's "Cut
status 'failed' is ambiguous by design" for the reasoning, and §5 below for why this ambiguity
needed its own staleness gates.

Pointers: `api/state.py`, `tests/test_state.py`.

---

## 3. Job lifecycle reliability

**What:** Every `Job`-backed task (`enrich_context`, `generate_guide`, `render_cut`,
`publish_cut`) runs through one shared decorator, `worker/tasks/common.py::job_task`, instead
of each task module reimplementing its own guard/retry/failure logic (which is exactly what
used to happen, and drifted — see CLAUDE.md's "`heartbeat()` and `job_task` live in
`worker/tasks/common.py`" convention).

**The shape**, each a compare-and-set so a worker that's lost the job can never clobber the
winner:
- **Atomic claim** — `UPDATE jobs SET status='running' WHERE id=? AND status='pending'`. Only
  one of two redelivered messages ever runs the body.
- **Idempotency** — `done`/`running` are redelivery no-ops; `failed` is terminal (an operator
  retry creates a *new* `Job` row, it never resurrects the old one — critical for `publish_cut`,
  where a resurrected run would upload the video twice).
- **Heartbeat** — a background thread refreshes `job.heartbeat_at` every 30s while the body
  runs (an LLM call or an ffmpeg render can block for minutes); the body itself also calls
  `heartbeat()` at milestones.
- **Reaper** — `reap_stuck_jobs` (Celery beat, every 60s) fails a `running` job whose heartbeat
  has gone stale (>5 min — a worker died) and a `pending` job that was never picked up at all
  (>240 min — a lost message or an unconsumed queue). Each reap is its own compare-and-set, and
  rolls back only the owner state that job type actually owns (`JOB_IN_FLIGHT` in
  `api/state.py`) — a stale render job never flips a `publishing` cut, for example.

**Why fencing (`Job.claim_token`) was needed — the false-positive-staleness problem:**
`reap_stuck_jobs` doesn't just fail a stuck `running` job of a resumable type
(`enrich`/`render`/`generate` — deliberately never `publish`, an irreversible external post) —
it puts it back to `pending` for a second worker to pick up, instead of only ever failing it.
The original design reasoned that this is always safe because a `running`-stale job's terminal
mutations can never have committed. True — but it silently assumed the reaper's only signal
(a stale heartbeat) actually means the process is dead. It doesn't always: a genuinely live body
blocked on one long LLM call, combined with a transient DB blip in the heartbeat thread's own
write, can go stale for 5+ minutes while still very much working. Resuming under that false
positive lets two executions of the same `Job` row run concurrently, and plain `status` can no
longer tell "the run that currently owns this row" apart from "a run that used to" — because the
resumed run's claim also satisfies `WHERE status='running'`.

`Job.claim_token` is a counter bumped on every `pending→running` claim; each run captures its
own token once at claim time, and six separate checkpoints (`heartbeat()`, `lock_job()`, the
done-stamp CAS, two failure-recording CAS sites, and the shutdown-signal path) compare against
it so a superseded run is told apart from the current one even while `status` alone would say
"running" for both. This went through three independent review passes — two at design time, one
on the merged code — each catching a genuine, distinct gap in the same mechanism. The full
blow-by-blow (all six checkpoints, four additional gaps found after the mechanism first shipped,
and why three specific CAS sites deliberately carry *no* token) lives in CLAUDE.md's "Fencing
token (`Job.claim_token`)" entry — read that before touching any of `job_task`'s failure paths.

Pointers: `worker/tasks/common.py`, `worker/tasks/maintenance.py::reap_stuck_jobs`,
`tests/test_job_lifecycle.py` (133 tests), `tests/test_maintenance.py` (52 tests),
`docs/specs/2026-09-reaper-resume-killed-jobs-system-design.md`.

---

## 4. Two-tier quality evaluation

**What:** A generated `MasterGuide` is scored two ways before it's accepted:
1. `score_guide()` (`engine/generation/evaluator.py`) — deterministic, 17-axis rule scorer
   (0–100), no LLM call. Decomposed (Phase 7s) into one `_score_<axis>(ctx)` function per axis
   sharing a `_ScoringContext`, from one ~650-line inline function.
2. `judge_guide()` (`engine/generation/llm_judge.py`) — an LLM semantic judge, 5 dimensions ×
   0–20, called only when the rule score clears 55 (no point paying for LLM judgment on a guide
   that's already structurally broken).

`combined = int(rule * 0.4 + llm * 0.6)`, accepted against a threshold (80 for NVIDIA
generation, 65 for local Ollama — NVIDIA-routed generation is measurably higher quality, so it's
held to a higher bar).

**Closed-loop retry:** on the standard LLM path, a failed attempt's issues (from both scorers)
are appended as a second user message on the *next* attempt — the LLM gets told specifically
what to fix, not just asked to try again blind. Up to 3 attempts; if none clears the threshold,
**best-of-3 is accepted rather than failing the job** — a mediocre guide beats no guide for an
unattended pipeline.

**Why decompose the scorer into per-axis functions:** `score_guide()`'s 17 axes used to live in
one function, provable only by driving the whole thing end-to-end. The decomposition (CAR-2, see
§8) made each axis independently testable — and that visibility immediately surfaced 5 real,
previously-undetected test-coverage gaps (sub-axis behaviors no black-box fixture had ever
isolated), closed with dedicated mutation-tested regression tests. See
`docs/specs/2026-09-score-guide-decomposition-module-design.md`.

Pointers: `engine/generation/evaluator.py`, `engine/generation/llm_judge.py`,
`worker/tasks/generate.py::_run_standard_path_attempts()`, `docs/evaluation.md` for the full
axis table and tuning guide.

---

## 5. Asset pinning, deterministic re-renders, and the two publish-time staleness gates

**Asset pinning (`resolve_or_reuse()`, `engine/render/asset_sourcer.py`):** each beat's sourced
assets are pinned to a `CutAsset` row keyed by `(cut_id, beat_index, order_in_beat)`, fingerprinted
by `sha256(visual_direction)[:16]`. A re-render only re-resolves (and re-pays for) a beat whose
`visual_direction` actually changed; every other beat reuses its existing pin with zero API
calls. This is what makes "edit one beat, re-render" fast and makes re-renders of an unchanged
guide byte-reproducible rather than a fresh roll of the asset-sourcing dice.

**Why that pinning needed its own publish-time gates:** pins commit incrementally, per beat,
inside the render loop — deliberately, so a crash mid-render leaves the *previous* valid pin in
place rather than a gap. But `Cut.video_path` is only written once, at the very end of a
*successful* render. If a render re-pins a beat and then fails before reaching that final write,
the database ends up with `CutAsset` describing the *new* resolution while `video_path` still
points at the *old* file — and `assert_safe_to_publish()` (which only reads current pins) has no
way to know they no longer describe the file about to ship. Combined with `CUT_TRANSITIONS`
allowing a `"failed"` cut to retry publish directly (§2), "Retry publish" on a failed cut could
ship a stale video with safety-irrelevant-looking pins.

The fix, applied twice, once for pins and once for guide content:
- **Pins fingerprint** (`Cut.rendered_pins_fingerprint`, Phase 7e) —
  `compute_pins_fingerprint_for_render()` snapshots a sha256 of the pins that built the
  *currently stored* video at the exact moment `render_cut` finishes successfully.
  `engine/publish/gate.py::assert_video_matches_pins()` recomputes it at publish time and raises
  on mismatch.
- **Guide fingerprint** (`Cut.rendered_guide_fingerprint`, Phase 7m) — the content-sibling gate:
  an operator can edit `cut.guide` (PATCH, or a hook-variant swap) and trigger a re-render that
  fails before completing, leaving a stale pre-edit video that still matches its (now stale)
  pins. `compute_guide_fingerprint(cut.guide)` is snapshotted the same way and compared by
  `assert_video_matches_guide()`.

Both gates treat `None` (pre-migration rows, or a cut that's never completed a render) as
"unknown — don't block," not a mismatch — a naive "any mismatch blocks" rule would have
immediately blocked publishing on every pre-existing rendered-but-unpublished cut in the
database the moment either shipped. Both also use a dedicated `_for_render` wrapper rather than
the raw fingerprint function, specifically so a successful render with zero bound pins (every
beat black-framed) is distinguishable from "never rendered" — an independent review caught that
gap before the pins gate merged (see CLAUDE.md's "Publish-time video/pins staleness gate" entry
for the full account, including why a fingerprint comparison was chosen over eagerly clearing
`video_path` on every re-render — the latter would destroy an approved video for *any* re-render
failure, not just an asset-safety-relevant one).

Pointers: `engine/render/asset_sourcer.py::resolve_or_reuse()`, `compute_pins_fingerprint()`,
`compute_pins_fingerprint_for_render()`; `engine/generation/guide_schema.py::compute_guide_fingerprint()`;
`engine/publish/gate.py`; `docs/specs/2026-09-video-pins-staleness-gate-system-design.md`,
`docs/specs/2026-09-stale-video-on-failed-rerender-system-design.md`.

---

## 6. Observability: `record_stage()` / `StageEvent`, and the "never fail the job, but still
tell the truth" composition pattern

**What:** `engine/observability.py::record_stage(db, reel_id, stage, ...)` is a context manager
wrapping any slow or paid call site (LLM generation, judging, asset sourcing, ffmpeg compositing,
a publish upload, a metrics pull). On exit it writes one `StageEvent` row — latency, provider,
model, token counts, `cost_usd`, `ok`, and a free-form `detail` JSON blob — whether the block
succeeded or raised.

**The composition pattern:** `record_stage()` does **not** swallow exceptions raised inside its
block — it records `ok=False` and then **re-raises**. That's correct for most call sites (a
failed LLM call should fail the job), but wrong for a step that must never fail its *enclosing*
job while still needing an honest, operator-visible failure signal. Two concrete examples, both
documented in CLAUDE.md as the canonical pattern to reuse:

- **YouTube captions upload** (`engine/publish/youtube.py`) — the video is already live by the
  time captions are attempted; a captions-only failure surfacing as "publish failed" would be
  actively wrong.
- **Instagram metrics drift detection** (`engine/publish/metrics.py`) — Meta has renamed Reels
  Insights metric names before; a renamed/retired metric should be recorded as a drift signal,
  not crash the 6-hourly metrics pull for every other cut.

The one composition that satisfies both "never fail the job" and "still tell the truth": put the
`try`/`except` **inside** the `with record_stage(...) as ev:` block, and on exception explicitly
set `ev.ok = False` plus a `detail` key describing what happened, then let the function return
normally (no re-raise). Both wrong compositions — catching outside the block (loses the `ok=False`
signal) and not catching at all (fails the enclosing job) — were mutation-tested during
implementation and confirmed to fail the dedicated regression tests before the correct version
was restored. See CLAUDE.md's "`record_stage()` composition for a 'never fail the job, but still
tell the truth' side effect" entry for the full account.

Pointers: `engine/observability.py`, `tests/test_tasks_real_db.py` (captions-upload coverage,
needs a real DB row), `tests/test_metrics_fetcher.py` (Instagram drift detection).

---

## 7. Internal refactoring history — a demonstrated commitment to deep, testable modules

This codebase treats its own internal structure as something worth continuously reviewing, not
just the features built on top of it. A full-repository architecture review surfaced several
"worth exploring" candidates, each implemented as its own PR, verified as a **pure refactor**
(all pre-existing tests pass unmodified against the decomposed code, checked *before* a single
test was touched) and then put through this project's standard 4-persona review (§9) on the
opened PR:

- **CAR-1 — `generate_guide()` decomposition** (`worker/tasks/generate.py`, Phase 7q): a single
  262-line, nine-responsibility function split into `_try_structured_path()` /
  `_run_standard_path_attempts()` / `_maybe_regenerate_caption_hashtags()` /
  `_maybe_generate_hook_variants()` / `_persist_guide()` orchestrated around one
  `_GenerationContext` dataclass. The review round caught one real, empirically-confirmed
  regression (caption/hashtags regeneration had gone from standard-path-only to unconditional)
  and two real test-coverage gaps before merge. `docs/specs/2026-09-generate-guide-decomposition-module-design.md`.
- **CAR-2 — `score_guide()` decomposition** (§4 above). `docs/specs/2026-09-score-guide-decomposition-module-design.md`.
- **CAR-3 — reader-ownership migration in the compositor** (`engine/render/compositor.py`,
  Phase 7r): manual `(clip, readers)`-tuple threading, which had already independently failed 5
  times across two prior PRs (a reader opened at some depth never made it into the list a caller
  could see — a real resource leak, not cosmetic), replaced with `contextlib.ExitStack` so every
  opened `VideoFileClip`/`AudioFileClip` registers itself the moment it's opened and gets closed
  on any unwind path, structurally — no function has to remember to propagate anything.
  `docs/specs/2026-09-compositor-reader-ownership-module-design.md`.
- **`guide_edit.py` extraction** (a later `improve-codebase-architecture` pass, PR #30): pulled
  ~75 lines of normalize/diff dict logic out of `api/routers/cuts.py::update_cut()` /
  `choose_hook_variant()` into a dedicated, DB-free, HTTP-agnostic module
  (`engine/generation/guide_edit.py`) — this logic already had two documented review-round bugs
  in CLAUDE.md before extraction, and had zero test surface of its own until this PR.
  `docs/specs/2026-09-guide-edit-module-design.md`.
- **`_generate_gated_hf_asset()` extraction** (PR #31): the near-identical gate/`record_stage`/
  cost-recording shell duplicated between the HF-video and HF-image tiers in
  `resolve_beat_assets()`, pulled into one narrow helper that owns only that shell — deliberately
  *not* a unified interface across all four asset sourcers, which was proposed and rejected
  during design review as moving code around without concentrating complexity anywhere real.
  `docs/specs/2026-09-hf-asset-gate-extraction-module-design.md`.

The common thread: every one of these was scoped narrowly (one real duplication or one
genuinely untestable concentration of responsibility), verified as behavior-preserving before
any test was touched, and still caught 1–5 real bugs or coverage gaps per round once reviewed —
evidence that "the tests still pass" and "the refactor is correct" are different claims, and
this project checks both.

---

## 8. Review discipline: implement → mutation-test → 4-persona review

The practice, applied to nearly every non-trivial change documented in CLAUDE.md's Key
conventions and Build phase status sections:

1. **Implement** against a design doc (`docs/specs/*-system-design.md` /
   `*-module-design.md`), itself often put through an adversarial review *before any code is
   written* — several of the gaps described in §3 and §5 above were caught at this stage, not
   after.
2. **Mutation-test the fix** — deliberately revert the specific change (remove a guard, swap a
   comparison, narrow a `try` block back to its pre-fix scope) and confirm the regression test
   written for it actually fails, for the predicted reason, before restoring the fix. A test that
   can't be made to fail this way is treated as not proving anything.
3. **4-persona review on the opened PR** (this project's default review depth per this user's
   own memory): **Security/Red-Team**, **Correctness/Edge-Case**, **Test-Quality Auditor**, and
   **Documentation-Consistency**, repeated until zero issues. This is not a formality — real
   examples from this codebase's own history: Security/Red-Team caught that `ExitStack`'s own
   unwind could let a reader's `close()` failure mask the original exception it was unwinding for
   (§7, CAR-3); Correctness/Edge-Case caught the caption-regeneration regression in CAR-1;
   Test-Quality Auditor repeatedly found tests that passed even against the exact bug they were
   meant to catch (vacuous tests) and demanded a reproduction of the failure before accepting a
   fix as verified.

New contributors should expect this cadence on anything touching job lifecycle, publish gating,
or the evaluator — not as bureaucracy, but because every one of those three areas has a
documented history of a first-draft fix shipping a real, found-later bug.

Pointers: `docs/specs/*-system-design.md` and `*-module-design.md` (each ends with its own
"Corrections" section recording what a later review round found), CLAUDE.md's Build phase status
for the narrative version of the same history.

---

## See also

- `docs/architecture.md` — module-by-module breakdown, full request-flow diagrams, guide schema,
  render pipeline stage-by-stage detail.
- `docs/data-model.md` — table definitions and state-machine diagrams.
- `docs/evaluation.md` — the full 17-axis scorer table and tuning guide.
- `docs/roadmap.md` — phase-by-phase build log.
- `CLAUDE.md` — the authoritative source for every design rationale summarized above, in full
  detail, including every review round's specific findings.
