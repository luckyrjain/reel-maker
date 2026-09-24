# Architecture

## Overview

Reel Maker is a local-first, single-operator pipeline that turns a text prompt into platform-specific short-form video. The system is designed around a single architectural principle: **every slow operation runs as an async background job**. Nothing slow happens inside a request cycle.

```
┌─────────────────────────────────────────────────────────────┐
│                      Browser (HTMX)                         │
│  form submit ──► POST /api/reels                            │
│  polls every 2s ◄── GET /api/reels/{id}/active-job-fragment │
│  render button ──► POST /api/cuts/{id}/render               │
│  polls every 3s ◄── GET /api/cuts/{id}/render-status        │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼──────────────────────────────────┐
│                   FastAPI (api/)                            │
│  Routers: reels · jobs · cuts                               │
│  Returns HTML fragments (Jinja2) — no JSON to browser       │
└────────────┬────────────────────────┬───────────────────────┘
             │ SQLAlchemy             │ Celery .delay()
┌────────────▼──────────┐  ┌──────────▼────────────────────────┐
│   PostgreSQL          │  │   Redis (broker + backend)        │
│   reels, cuts, jobs,  │◄─┤   visibility_timeout=7200 s       │
│   assets, cut_assets, │  └──────────┬────────────────────────┘
│   stage_events,       │             │ task execution
│   credentials         │  ┌──────────▼────────────────────────┐
└───────────────────────┘  │   Celery Worker (worker/)         │
                           │   enrich_context  [generation Q]  │
                           │   generate_guide  [generation Q]  │
                           │   render_cut      [rendering Q]   │
                           │   reap_stuck_jobs [generation Q]  │
                           └──┬────────────────┬───────────────┘
                              │                │
             ┌────────────────▼──┐   ┌─────────▼──────────────────────────┐
             │  engine/          │   │  External services                  │
             │  generation/      │   │  Ollama / LM Studio (main LLM)     │
             │  render/          │   │  NVIDIA NIM (enrichment + judge)   │
             │  observability.py │   │  Pexels API (stock footage)        │
             └───────────────────┘   │  Wikipedia REST (player photos)    │
                                     │  Edge TTS (Microsoft neural)       │
                                     │  Whisper (optional, word timing)   │
                                     └────────────────────────────────────┘
```

---

## Request / response pattern

The browser never fetches JSON. All API responses to the browser are HTML fragments (Jinja2 templates). HTMX swaps these fragments into the DOM.

**Generation flow:**
1. Form `POST /api/reels` (multipart form data — includes `generation_path`)
2. API creates `Reel` + `Cut` rows + `Job(type=enrich)` row, transitions reel to `enriching`, enqueues `enrich_context`, returns `fragments/pipeline_status.html`
3. Fragment polls `GET /api/reels/{id}/active-job-fragment` every 2 s — always returns the currently active job (enrich or generate), so the UI tracks seamlessly through both phases
4. `enrich_context`: scores context (5-axis rule scorer, 0–100); if score < 60, calls LLM to enrich; stores result in `reel.enriched_context`; creates + enqueues a `Job(type=generate)`
5. `generate_guide` picks up with `effective_context = reel.enriched_context or reel.context`
6. When generate job is `done`, fragment shows "Guide ready · quality score NN/100 · context score NN/100 →"; when `failed`, shows the error

**Render flow:**
1. Button `POST /api/cuts/{id}/render`
2. API transitions cut status, creates `Job`, enqueues task, returns `fragments/render_status.html`
3. Fragment polls `GET /api/cuts/{id}/render-status?job_id={id}` every 3 s
4. When done, shows `<video>` player + **Approve ✓** and **Re-render** buttons

**Review & edit flow:**
1. Cut is in `in_review` — cut card renders as an editable form
2. User edits beat fields (visual direction, VO script, on-screen text), caption, or hashtags
3. `PATCH /api/cuts/{id}` — updates `cut.guide`, `cut.caption`, `cut.hashtags` in DB, returns fresh `cut_card.html`
4. Re-render: only beats whose `visual_direction` changed are re-resolved (others reuse pinned assets)
5. Approve: `POST /api/cuts/{id}/approve` transitions cut to `approved`

---

## Module breakdown

### `api/`

| File | Responsibility |
|---|---|
| `main.py` | FastAPI app factory; mounts static files, Jinja2 templates, all routers |
| `config.py` | `pydantic-settings` `Settings`; reads `.env`; includes `nvidia_api_key`, `credentials_key` |
| `crypto.py` | Fernet `seal()`/`open_()` helpers + `Encrypted` SQLAlchemy TypeDecorator for encrypted columns |
| `db.py` | SQLAlchemy engine, `SessionLocal`, `get_db()` FastAPI dependency |
| `models.py` | All ORM models and status enums; includes `StageEvent` |
| `schemas.py` | `JobResponse` — the one JSON endpoint's response shape |
| `state.py` | `REEL_TRANSITIONS`, `CUT_TRANSITIONS` dicts + `transition()` guard; `JOB_IN_FLIGHT` — the owner state each job type rolls back to `failed` |
| `routers/reels.py` | `POST /api/reels` — creates reel, enqueues `enrich_context`; `GET /api/reels/{id}/active-job-fragment` — reel-level polling; `GET /api/reels/{id}` |
| `routers/jobs.py` | `GET /api/jobs/{id}` (JSON) — the only JSON endpoint |
| `routers/cuts.py` | `POST /api/cuts/{id}/render`, `PATCH /api/cuts/{id}`, `POST /api/cuts/{id}/approve`, render-status, video stream |

### `worker/`

| File | Responsibility |
|---|---|
| `celery_app.py` | Celery instance; `task_acks_late=True`, `visibility_timeout=7200`, beat schedule, split queues |
| `tasks/common.py` | `job_task()` — the shared Job lifecycle decorator every task body runs under (only-`pending` guard → atomic claim → heartbeat thread → `prepare` → body → fenced done-stamp → `after_commit`; on error, retry-reset or failure stamp + per-job-type owner rollback from `api/state.py::JOB_IN_FLIGHT`, every write a compare-and-set); `rollback_owner()`; `is_transient_error()` / `should_retry()` — retry classification; `heartbeat()` — shared progress write |
| `tasks/enrich_context.py` | `enrich_context(job_id)` — `evaluate_context()` → `script_parser.is_structured()` guard → optional `llm_enrich()` (skipped for structured scripts) → stores `reel.enriched_context` → creates + enqueues generate job |
| `tasks/generate.py` | `generate_guide(job_id)` — `prepare` (missing-row guard, reel must be `generating`, paid-call budget) → `effective_context = enriched_context or context` → structured/standard path → closed-loop eval retry → DB writes; retries transient failures twice |
| `tasks/render.py` | `render_cut(job_id)` — `prepare` (missing-row guards) → heartbeat → `resolve_or_reuse()` per beat → `synth_to_budget()` → TTS-accurate timecodes → atomic MP4; retries transient failures twice |
| `tasks/maintenance.py` | `reap_stuck_jobs()` — Celery beat, every 60 s; fails `running` jobs with stale heartbeat (> 5 min) and `pending` jobs never picked up (> 240 min, `PENDING_STALE_MINUTES`); routed to the `generation` queue with a 55 s message expiry; each reap is a compare-and-set that rolls back only the owner state the job's type owns |

### `engine/`

| File | Responsibility |
|---|---|
| `observability.py` | `record_stage()` context manager — writes `StageEvent` on exit (success or failure) |

#### `engine/generation/`

| File | Responsibility |
|---|---|
| `guide_schema.py` | `Beat`, `PlatformGuide`, `MasterGuide` Pydantic models; `coerce_beat_type` validator |
| `llm.py` | `LLMProvider` + `OllamaProvider`; `get_llm_provider()`, `get_enrichment_provider()`, `is_nvidia_generation()` |
| `prompt.py` | `build_messages(prior_feedback=)` — accepts prior failure issues for closed-loop retry; `build_visuals_messages()` — system prompt anchors LLM to per-beat VO only, preventing drift to global context |
| `script_parser.py` | `parse(context)` — splits on `PlayerName: text`, returns `list[BeatStub]` or `None`; `is_structured(context)` — the single ≥3-labelled-sections test, also used by the enrichment guard; `calc_duration()`, `derive_on_screen()` public helpers |
| `context_enricher.py` | `evaluate_context()` — 5-axis rule scorer (0–100); `llm_enrich()` — single LLM call to add specificity, stakes, hook angle; `ENRICH_THRESHOLD = 60` |
| `evaluator.py` | `score_guide()` — 17-axis rule scorer; see `docs/evaluation.md` |
| `llm_judge.py` | `judge_guide()` — LLM semantic judge; 5 dimensions × 0–20; fallback to (50, [warning]) on failure |
| `postprocess.py` | `clean_guide()` — strips label prefixes; derives up to 5 on_screen_text segments |
| `visual_fallback.py` | `_fallback_visual()`, `_first_person()`, `_is_degenerate_visual()` — synthesizes a usable `visual_direction` when the LLM returns a degenerate one |
| `beat_enrichment.py` | `_enrich_with_insight()`, `_make_conflict_stub()`, `_has_conflict_beat()` — topic-fenced beat enrichment and conflict-beat synthesis |

#### `engine/render/`

| File | Responsibility |
|---|---|
| `asset_sourcer.py` | `PexelsVideoSource` + `WikipediaImageSource` (including `extmetadata` license fetch) + `HuggingFaceVideoSource` + `HuggingFaceImageSource`; fallback chain Wikipedia → Pexels → HF video → HF image → black frame; `resolve_or_reuse()` — pins assets per beat, reuses without API call when fingerprint matches; all downloads go through `_atomic_write()` |
| `tts.py` | `EdgeTTSProvider.synthesize(rate=)` + `.synth_to_budget(target_s)` — adjusts speaking rate ±25% to hit duration budget; `_audio_duration()` via ffprobe |
| `captions.py` | `transcribe_audio()` — Whisper word-level timestamps → `list[CaptionSegment]`; model cached per process via `@lru_cache`; no-op if whisper not installed |
| `compositor.py` | `composite_cut()` — MoviePy stage (video + audio) + FFmpeg drawtext stage (text overlays); 120ms audio fade in/out per beat (`AudioFadeIn`/`AudioFadeOut` via `.with_effects()`) for smooth narration transitions; `_build_text_filter()` uses Whisper timestamps when available, proportional fallback otherwise; atomic `os.replace()` for final output |

### `tests/`

| File | Coverage |
|---|---|
| `test_evaluator.py` | 29 tests — all 17 evaluator axes + helpers, multi-platform de-duplication |
| `test_script_parser.py` | 11 tests — parse routing, player splitting, `derive_on_screen`, `calc_duration` |
| `test_state.py` | 11 tests — valid/invalid transitions for both state machines including `enriching` |
| `test_enrichment.py` | 15 tests — `coerce_beat_type`, `_enrich_batch` response shapes, topic fence assertions |
| `test_audio_text_sync.py` | 11 tests — `clean_guide()` regeneration, `_build_text_filter()` proportional timing + Whisper fallback, visual direction anchoring |
| `test_context_enricher.py` | 13 tests — all 5 evaluator axes at boundary values, combined score, `llm_enrich` |
| `test_enrich_context_task.py` | 14 tests — enrichment gating, LLM failure fallback, structured script guard, missing reel, owner rollback wiring, orphan cleanup |
| `test_job_lifecycle.py` | 116 tests — `job_task` on dummy tasks: atomic claim, fenced done-stamp/heartbeat, heartbeat thread, retry/failure/owner rollback, fail-fast on shutdown/hard-kill and a refused retry, dead-connection recovery in the terminal failure recorders (incl. InterfaceError, fresh-session close on success and on a failed retry), a cleanup hook that itself raises without discarding the failure stamp (incl. a multi-write hook rolling back atomically via its own SAVEPOINT), `.delay` signature regression, per-task wiring, beat routing |
| `test_r3_proposed.py` | 38 tests — round-3 mutation-testing regressions: distinct job/reel/cut ids, transaction visibility via a second connection, `_error_text` regex boundaries, template `hx-post` assertions |
| `test_r4_gaps.py` | 30 tests — round-4 mutation-testing regressions: heartbeat commit visibility, failure-path rollback of flushed rows, `after_commit` cleanup without a hook |
| `test_tasks_real_db.py` | 4 tests — real tasks through `job_task`: post id durable, caption sent, enrich enqueues the real job id |
| `test_common.py` | 18 tests — transient-error classification (incl. DB connection errors), retry budget boundary |
| `test_generate_task.py` | 9 tests — missing reel, reel not generating, paid-call budget, structured-path fallback (incl. a soft-limit kill), `music_cue` default, caption/hashtags does not swallow a runtime-limit timeout |
| `test_publish_task.py` | 10 tests — safety gate, no auto-retry, early post id, finalize without re-upload, attribution |
| `test_render_task.py` | 6 tests — missing cut, already-posted cut refused, success clears stale error, music wiring |
| `test_maintenance.py` | 29 tests — reaper on SQLite: per-job-type rollback and pending thresholds, compare-and-set back-off, status pin |
| `test_cuts_publish_router.py` | 22 tests — POST /cuts/{id}/publish state-guard and enqueue; render refused for an already-posted cut; enqueue fails fast (503) and frees the cut; row lock incl. `update_cut` body-before-lock ordering; failed-cut card |
| `test_asset_sourcer.py` | 7 tests — `resolve_or_reuse` pin, reuse-without-API-call, re-pin, per-beat isolation, commit behaviour, Wikipedia search-before-cache ordering |
| `test_llm_judge.py` | 3 tests — neutral-score fallback on provider raise, garbage JSON, out-of-range dimension |
| `test_tts.py` | 8 tests — provider selection, unknown-provider fallback, `SilentProvider` shared file, `synth_to_budget` clamp |

**539 tests across 37 files.**

---

## Guide generation — two paths

### Pre-generation context evaluation and enrichment

Before generation begins, `enrich_context` runs a rule-based quality check on the raw input:

```
reel.context
  └── evaluate_context()   →  (score, issues)   [5 axes × 20 pts = 100 max]
        └── script_parser.is_structured()  →  bool (≥3 labelled sections)
              ├── if score < 60 AND NOT structured:
              │     llm_enrich()   →  enriched text stored in reel.enriched_context
              │     record_stage("context_enrich")
              └── if structured: skip enrichment (context topic is already locked in)
        └── create Job(generate) + enqueue generate_guide

generate_guide:
  effective_context = reel.enriched_context or reel.context
  (all generation, scoring, and evaluation use effective_context)
```

**5 scoring axes:** Length (word count) · Specificity (named entities + numbers) · Stakes/tension (conflict vocabulary) · Narrative arc (discourse connectors) · Hook potential (question / direct address / bold claim in first sentence). Score < 60 triggers enrichment; original context always preserved.

**Structured script guard:** `enrich_context.py` imports `script_parser.is_structured()` — the same ≥3-labelled-sections test `parse()` applies, so the guard and the parser can never disagree about the same input. When True, `llm_enrich()` is skipped entirely — the script's topic is already locked in and enrichment would cause context drift. `job.meta["enrich_skipped"]` records the reason (`"structured_script"` or `"score_above_threshold"`).

---

### Path selection

The form's "Generation path" dropdown sends `generation_path=auto|structured|standard`. The router stores this in the enrich job's `meta["generation_path"]`; `enrich_context` copies it into the generate job's meta. The task reads it:

- `standard` — forces standard LLM path even if context has ≥3 headers
- `structured` — forces structured path even if headers are missing
- `auto` (default) — detects from context via `script_parser.parse()`

`job.meta["path"]` records which path was actually taken; `job.meta["stub_count"]` records the number of beats parsed.

### Structured-script path (~60–120 s)

```
context
  └── script_parser.parse()
        └── list[BeatStub]
              ├── _enrich_with_insight()   # enrichment LLM — tactical sentences (NVIDIA NIM or Ollama)
              │     └── record_stage("enrich")
              └── _make_conflict_stub()    # if no conflict/tension beat exists
                    └── build_visuals_messages()
                          └── main LLM (visuals only)
                                └── _stubs_to_platform_guide()
                                      └── MasterGuide
```

**Enrichment**: beats with < 25 words or no causal language get one tactical insight sentence appended, batched in groups of 3. `_enrich_batch()` handles both single-dict and list LLM responses.

**Conflict injection**: if no body beat contains tension words, `_make_conflict_stub()` generates a 2–3 sentence weakness beat. Inserted before the CTA.

**Visual fallback**: `_is_degenerate_visual()` catches empty or boilerplate visuals. `_fallback_visual()` maps VO keywords to specific shot types via `_VO_TO_SHOT`.

### Standard LLM path (2–5 min)

```
context + prior_feedback (if retry)
  └── build_messages(prior_feedback=issues)
        └── main LLM (full MasterGuide JSON)
              └── MasterGuide.model_validate_json()
                    ├── clean_guide()
                    └── score_guide() + judge_guide()   # two-tier eval
                          ├── record_stage("generate")
                          ├── record_stage("judge")
                          ├── combined ≥ threshold → accept   (80 NVIDIA / 65 local Ollama)
                          ├── combined < threshold → feedback = issues; retry (up to 3×)
                          └── if all 3 fail → accept best-of-3
```

**Closed-loop retry**: failure issues from `score_guide()` + `judge_guide()` are appended as a second user message in the next attempt. The LLM receives actionable instructions on what to fix. If no attempt clears 80/100, the highest-scoring guide is accepted rather than failing the job.

### Two-tier quality evaluation

```
score_guide()     deterministic, 17 axes (max deductions exceed 100; score clamped 0–100):
  Retention Architecture  20 pts   (hook strength + open loops + momentum shifts)
  Narrative Quality       15 pts   (HOOK→CONTEXT→ANALYSIS→CONFLICT→CONCLUSION arc)
  Context Coverage        10 pts   (≥50% of source sentences echoed in VO)
  Insight Density         15 pts   (stats, causal language, tactical terms, comparisons)
  Script↔Visual Align     20 pts   (entity + action + context match)
  Clip Availability       10 pts   (visual_direction describes sourceable footage)
  Visual Editability      10 pts   (specific enough for automation)
  Emotional Impact        13 pts   (density 10 + distribution across beats 3)
  Audio Delivery          10 pts   (WPS range, hook capped tighter + sentence length + rhythm)
  Visual Variety           5 pts   (mix of shot types across beats)
  Duration Fit             5 pts   (beat durations within ±30% of target, per cut)
  Caption & Hashtag        5 pts   (caption ≥30 chars, ≥10 hashtags, per cut)
  CTA Action               3 pts   (quality-weighted: prediction/opinion > passive follow)
  Conversational Tone     10 pts   (penalise encyclopaedic phrasing; reward direct address)
  Hook-CTA Throughline     5 pts   (CTA references the hook's tension or player)
  Per-Beat Specificity     5 pts   (each body beat makes a falsifiable claim)
  Repetition               5 pts   (body beats use distinct vocabulary)

  Beat-level axes de-duplicate beats across guide.cuts by
  (index, vo_script, visual_direction) — both platform guides normally hold
  identical beats, and counting them twice inflates the capped axes and pairs
  each beat against its own clone. Per-cut axes (duration, caption, hashtags)
  deliberately deduct once per platform.

  if rule ≥ 55:
    judge_guide()  LLM semantic, 5 dimensions × 0–20:
      Factual accuracy · Expertise depth · Natural speech
      Hallucination risk · Shareability

combined = int(rule × 0.4 + llm × 0.6)   threshold 80 (NVIDIA) / 65 (local Ollama)
```

---

## Reliability

### Celery configuration (`worker/celery_app.py`)

| Setting | Value | Reason |
|---|---|---|
| `task_acks_late` | `True` | Ack only after task returns — killed worker requeues (the redelivered message finds the job `running`/`failed` and no-ops; recovery is the reaper failing the job, then an operator retry) |
| `task_reject_on_worker_lost` | `True` | SIGKILL redelivers the message rather than dropping it; the redelivery finds the job `running` (or already `failed` by the reaper) and no-ops — recovery is the reaper failing the job, not an automatic re-run |
| `visibility_timeout` | 7200 s | Outlasts normal broker/worker hiccups; a redelivery of a still-running job is a safe no-op via the atomic claim, so this need not exceed every task's `max_runtime_s` (generate's cap is 4 h) |
| `worker_prefetch_multiplier` | 1 | No worker hoards multiple long tasks |
| `worker_max_tasks_per_child` | 10 | Respawn render workers to reclaim MoviePy/ffmpeg memory |
| `max_retries` | 2 | Real, via `should_retry()` — 30 s/60 s backoff on transient failures only; `enrich_context` and `publish_cut` stay at 0 (publishing is an irreversible external post) |

### Idempotency

`job_task` lets only a `pending` job run: `done`/`running` are redelivery no-ops, and `failed` is terminal (an operator retry creates a new Job, so a late redelivery of a reaped job must not run — for publish it would upload the video). The claim is an atomic `UPDATE … WHERE status = 'pending'`, so of two deliveries only one runs the body, and the done-stamp is fenced on `status = 'running'` so a worker the reaper already gave up on cannot commit its result.

This is also why the retry branch resets `job.status` to `pending` before calling `self.retry()`: a retry that left the status at `running` would be rejected by the claim on redelivery, making every retry a silent no-op.

### Heartbeat + stuck-job reaper

`job_task` refreshes `job.heartbeat_at` every 30 s from a background thread while the body runs (an LLM call or a render can block for minutes), and bodies call `heartbeat(db, job, progress)` — from `worker/tasks/common.py`, never redefined per task — for progress.

`reap_stuck_jobs` (Celery beat, 60 s interval) fails two kinds of stalled job:

- `status=running` with `heartbeat_at` older than 5 minutes — worker killed mid-task.
- `status=pending` with `updated_at` older than 240 minutes (`PENDING_STALE_MINUTES`) — never picked up at all (a message lost after a successful enqueue, or no worker consuming the queue). Deliberately long: a job legitimately queues behind hour-long renders (concurrency 1) or busy generation slots, and a reaped job is terminal. A router whose `.delay()` raised fails its job immediately (`fail_unenqueued`) instead of waiting for this. Keyed on `updated_at`, not `created_at`, so a job sitting in retry backoff is not reaped for being old.

Each reap is a compare-and-set that re-checks staleness in the UPDATE (a job that beat or finished after the SELECT is left alone), and rolls back only the owner state the job's type owns (`JOB_IN_FLIGHT`). The task is routed to the `generation` queue; a beat task with no route lands on the default queue that no documented worker consumes.

### Atomic file writes

The compositor writes FFmpeg output to `{out}.tmp.mp4`, then `os.replace()` to the final path. A killed process never leaves a servable half-written video.

The same rule applies to every asset download. Pexels streams to `.tmp`; Wikipedia and both HuggingFace sources go through `_atomic_write()`. Each sourcer caches by `if path.exists()`, so a truncated file would otherwise be reused on every later render.

**TTS duration override caveat:** `render_cut` replaces each beat's `duration_s` with the measured audio length, but skips this when the active provider is `SilentProvider` — that provider returns the same 1-second placeholder for every beat, so measuring it would collapse the whole reel to ~1 s per beat.

---

## Observability

`engine/observability.py` provides `record_stage()` — a context manager that writes a `StageEvent` row on exit:

```python
with record_stage(db, reel_id, "judge", provider="nvidia", model=MODEL, attempt=1) as ev:
    score, reasons = judge_guide(context, guide, llm)
    ev.score = score
    ev.detail["reasons"] = reasons
```

Wired at: `context_enrich`, `enrich`, `generate` (each retry), `judge` (each retry), `composite`, `asset_hf_video`, `asset_hf_image`, `publish`.

`record_stage()` commits the session it is given, so every call site must sit on a commit boundary — never wrap a half-applied mutation in it. This bit `resolve_or_reuse()` in `asset_sourcer.py`: it now resolves (which may commit mid-way via `record_stage`) *before* deleting stale `CutAsset` pins, not after — a crash mid-resolve leaves the old-but-valid pin in place rather than no pin at all.

`StageEvent` fields: `stage`, `provider`, `model_name`, `latency_ms`, `tokens_in`, `tokens_out`, `cost_usd`, `attempt`, `score`, `ok`, `detail` (JSON), `created_at`. `cost_usd` is computed for NVIDIA LLM calls (`engine/generation/pricing.py`) and for HF asset generation (`engine/render/pricing.py`) — both `0.0` until the operator sets a real rate; `asset_hf_*` stages only charge `cost_usd` when the call actually generated an asset, not on a cache hit.

---

## Per-beat asset pinning

`resolve_or_reuse()` in `asset_sourcer.py` provides deterministic re-renders:

1. Computes `fingerprint = sha256(visual_direction)[:16]`
2. Queries `cut_assets` for existing pins for this `(cut_id, beat_index)`
3. If all pins have matching `resolved_from`: returns pinned assets directly (no API call)
4. Otherwise: calls `resolve_beat_assets()`, deletes stale pins, inserts new ones with the fingerprint

This means editing beat 3's VO and re-rendering only triggers a new API call for beat 3; all other beats reuse cached pins. The `CutAsset` unique constraint `(cut_id, beat_index, order_in_beat)` enforces one binding per slot.

---

## State machines

### Reel (`REEL_TRANSITIONS`)

```
draft ──► enriching ──► generating ──► guide_ready
              │               │
              └──► failed ◄───┘
                       │
                       └──► draft
```

### Cut (`CUT_TRANSITIONS`)

```
draft ──► rendering ──► in_review ──► approved ──► publishing ──► published
              │              │                            │
              │              └──► rendering               └──► scheduled ──► publishing
              └──► failed ──► draft
```

---

## Render pipeline detail

`composite_cut()` runs two stages:

**Stage 1 — MoviePy (video + audio, no text):**
```
For each beat:
  resolve_or_reuse() → media file(s) for this beat
  synth_to_budget(vo_script, target_s=beat.duration_s) → .mp3
    ↳ adjusts speaking rate ±25% if measured duration drifts > 15%

  For each media_path:
    image → fit 9:16, Ken Burns zoom (pre-computed keyframes)
    video → scale/crop 1080×1920, loop if too short, trim

  concatenate sub-clips → beat_clip
  VO audio: AudioFileClip → subclip if too long → .with_effects([AudioFadeIn(0.12), AudioFadeOut(0.12)]) → .with_start(t)

After all beats:
  concatenate beats → final video
  position each VO audio at beat offset → CompositeAudioClip
  save frame at 0.5 s as thumbnail.jpg
  write_videofile → .notxt.mp4 (libx264, aac, 30 fps)
```

**TTS duration override**: `ffprobe -show_streams` measures actual synthesized audio duration; `beat_dict["duration_s"] = actual + 0.1 s`. This overrides the guide's word-count estimate.

**CutAsset timing**: `start_s`/`end_s` are updated via SQL UPDATE after TTS measurement, so they reflect actual rendered timecodes.

**Stage 2 — FFmpeg drawtext (timed text overlays):**
```
For each beat:
  if Whisper transcripts available:
    divide word stream proportionally across on_screen_text lines → (start, end) per line
  else:
    proportional word-count timing

ffmpeg -i .notxt.mp4 -vf "<drawtext filter chain>" -c:a copy .tmp.mp4
os.replace(.tmp.mp4, output.mp4)
unlink .notxt.mp4  (in finally block)
```

Font: 60px white, black shadow, centered at 73% of frame height. Up to 5 segments per beat.

---

## Asset sourcing

### Stock footage (`PexelsVideoSource`)

- Portraits at ≤ FHD (1920 px) preferred; falls back gracefully to landscape or 4K
- Cached by `(source="pexels", source_ref=video_id)` in `assets` table
- `safe_to_publish=True` (Pexels license is permissive)

### Player photos (`WikipediaImageSource`)

- Triggered when `_NAME_RE` finds a multi-word capitalized name in `visual_direction`
- Flow: opensearch → page summary REST → image download + `extmetadata` license fetch
- Rate-limit handling: 0.5 s between players, 2 s retry on 429
- License fields stored: `license`, `license_url`, `attribution`, `safe_to_publish`
- `safe_to_publish` is `True` only for CC0/CC-BY/public domain — most player headshots are CC-BY-SA (attribution required at publish)
- Multiple players in one beat → multiple assets, cycled as Ken Burns sub-clips

### TTS audio

`tts_provider` config defaults to `"edge"`. Valid values are `edge`, `kokoro`, and `silent`; anything else logs a warning and falls back to `SilentProvider` (no audio).

`EdgeTTSProvider` (activated by `TTS_PROVIDER=edge`):
- `_normalize_for_tts()`: contraction restoration → acronym expansion → diacritic stripping
- `synthesize(text, rate="+0%")`: cache key = `sha256(voice + rate + text)[:20]`
- `synth_to_budget(text, target_s)`: synthesizes at default rate, measures duration, re-synthesizes with adjusted rate if drift > 15%

`KokoroProvider` (activated by `TTS_PROVIDER=kokoro`, requires Python < 3.13):
- Local neural TTS; higher quality than Edge TTS but incompatible with Python 3.14

---

## LLM provider routing

```python
get_llm_provider()
  if NVIDIA_API_KEY set
  and USE_NVIDIA_FOR_GENERATION: →  OllamaProvider(NVIDIA_BASE_URL, NVIDIA_GENERATION_MODEL, api_key=key)
  else:                          →  OllamaProvider(LLM_BASE_URL, LLM_MODEL)
                                    default: qwen3:14b @ localhost:11434/v1

is_nvidia_generation()      →  True when the above routed to NVIDIA; selects the
                               quality threshold (80 vs 65) and StageEvent provider label

get_enrichment_provider()
  if NVIDIA_API_KEY set:    →  OllamaProvider(NVIDIA_BASE_URL, NVIDIA_ENRICHMENT_MODEL, api_key=key)
                               default: qwen/qwen3-next-80b-a3b-instruct @ integrate.api.nvidia.com/v1
  else:                     →  OllamaProvider(LLM_BASE_URL, LLM_ENRICHMENT_MODEL)
                               default: qwen3:14b @ localhost:11434/v1
```

`OllamaProvider` is fully OpenAI-compatible (`/chat/completions`). The `api_key` adds `Authorization: Bearer {key}`.

---

## Guide schema

```python
Beat:
  index: int
  type: "hook" | "body" | "cta"   # coerced from unknown strings
  duration_s: float                # sum ≈ target_length_s; overridden by actual TTS duration at render
  visual_direction: str            # must start with player FULL NAME to trigger Wikipedia
  on_screen_text: list[str]        # up to 5 items, max 7 words each; shown sequentially
  vo_script: str                   # TTS input; empty if music-only
  music_cue: str | None            # mood keyword; matched to a local library track by LocalMusicSource
  transition: "cut" | "fade" | "slide"

PlatformGuide:
  platform: "youtube_shorts" | "instagram_reels"
  target_length_s: float
  beats: list[Beat]               # min 3; first=hook, last=cta
  caption: str
  hashtags: list[str]             # 5–25, no # prefix

MasterGuide:
  title: str
  niche: str
  cuts: list[PlatformGuide]       # one per requested platform
```

---

## What is not yet built

- **TikTok publishing**: `CutPlatform.tiktok` exists (render/review works); `TikTokPublisher.publish()` raises `NotImplementedError` on purpose — the Content Posting API needs a separate audited app review, unlike YouTube/Instagram's self-serve OAuth
- **Scheduling**: `scheduled` cut status and `publish_cut` both handle a cut already sitting in `scheduled`; no UI/scheduler worker transitions a cut *into* it yet
- **Multi-image collage**: cycles sequentially; no side-by-side layout within a beat
- **Word-level caption export**: Whisper timing drives on-screen text, but no SRT/VTT file is generated for the platform uploader
- **Metrics time series**: `cuts.views`/`likes`/`comments`/`metrics_updated_at` hold only the latest pull, not a history — see `docs/roadmap.md` Phase 5b

YouTube + Instagram publishing (OAuth, `safe_to_publish` gate, upload flows) shipped
in Phase 4b — see `engine/publish/`, `api/oauth.py`, `worker/tasks/publish.py`.

Music mixing, HF asset-generation cost tracking, caption attribution, and
post-publish metrics pull-back shipped in Phase 5 — see
`engine/render/asset_sourcer.py::LocalMusicSource`,
`engine/render/compositor.py::_build_ffmpeg_args()`,
`engine/render/pricing.py`, `engine/publish/attribution.py`,
`engine/publish/metrics.py`, `worker/tasks/metrics.py`, and
`docs/roadmap.md` Phase 5 for the reasoning behind each deviation from the
original plan.
