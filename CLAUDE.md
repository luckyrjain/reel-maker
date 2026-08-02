# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Stack

All-Python: FastAPI + Celery/Redis + PostgreSQL + SQLAlchemy/Alembic + MoviePy/FFmpeg + Pillow. No Node. UI is server-rendered Jinja2 + HTMX (no SPA, no build step).

## Runtime requirements

- **Starlette ≥ 0.36 / 1.x** — `TemplateResponse` takes `request` as the first positional argument, not inside the context dict. All template calls use `templates.TemplateResponse(request, "template.html", {...})`.
- **LLM — two tiers:**
  - `LLM_MODEL` (main generation, default `qwen3:14b`) — used for full guide generation and visuals prompts. Any OpenAI-compatible model works. The 3.2B `llama3.2` model is too small — it fails schema validation. Test a new model with one generation before relying on it.
  - `LLM_ENRICHMENT_MODEL` (enrichment + judge, default `qwen3:14b`) — used for `_enrich_with_insight()`, `_make_conflict_stub()`, and the LLM quality judge. Requires a capable model that returns reliable JSON arrays. If `NVIDIA_API_KEY` is set, enrichment is automatically routed to `NVIDIA_ENRICHMENT_MODEL` (default `qwen/qwen3-next-80b-a3b-instruct`) at NVIDIA NIM instead.
  - Set `USE_NVIDIA_FOR_GENERATION=true` in `.env` to route main guide generation to NVIDIA NIM (`NVIDIA_GENERATION_MODEL`, default `qwen/qwen3-next-80b-a3b-instruct`). Produces significantly higher quality (81/100 vs 50/100 measured). Uses `is_nvidia_generation()` helper in `llm.py` to select provider and adaptive threshold.
- **LLM timeout** — `OllamaProvider` uses a 360 s HTTP timeout (`llm.py`). Smaller local models can take 3–5 min per call.
- **NVIDIA NIM** — set `NVIDIA_API_KEY` in `.env` to route enrichment, conflict-beat generation, and LLM judge calls to `https://integrate.api.nvidia.com/v1`. The `NVIDIA_ENRICHMENT_MODEL` defaults to `qwen/qwen3-next-80b-a3b-instruct`. Leave blank to use local Ollama.
- **HuggingFace asset generation** — set `HUGGINGFACE_API_KEY` in `.env`. `HuggingFaceVideoSource` (LTX-Video, `HUGGINGFACE_VIDEO_MODEL`) generates short clips when Pexels finds nothing; `HuggingFaceImageSource` (FLUX.1-schnell, `HUGGINGFACE_IMAGE_MODEL`) generates static images as a further fallback. Asset fallback chain: Wikipedia → Pexels → HF Video → HF Image → black frame. Both cache locally by prompt fingerprint. HF image `generate()` checks `content-type` starts with `image/` before writing — JSON error bodies (200 OK "model loading") are rejected. HF video cache checks both `.mp4` and `.gif` extensions.
- **Credential encryption** — `crypto.py` `open_()` raises `ValueError` on decryption failure (e.g. rotated key) rather than silently returning the raw ciphertext.
- **TTS** — `tts_provider` in config defaults to `"edge"`. Valid values: `edge`, `kokoro`, `silent`; anything else logs a warning and falls back to `SilentProvider`. `SilentProvider` returns one shared 1 s file for every beat, so `render_cut` skips the "measure TTS length" duration override when it is active — measuring it would collapse the whole reel to ~1 s per beat. `edge-tts` uses Microsoft Edge neural voices (free, Python 3.14 compatible) and must be installed separately (`pip install edge-tts`). Kokoro TTS (`TTS_PROVIDER=kokoro`) requires Python < 3.13. Default Edge voice: `en-GB-RyanNeural`.
- **TTS text normalization** — `_normalize_for_tts()` in `tts.py` runs three passes: (1) restores missing apostrophes in contractions (isnt → isn't, doesnt → doesn't — LLMs frequently omit them), (2) expands short words that get spelled as acronyms (`Emi` → `Emmy`), (3) strips Unicode diacritics so accented names (Martínez, Álvarez) are pronounced naturally by an English neural voice.
- **TTS budget control** — `EdgeTTSProvider.synth_to_budget(text, target_s)` re-synthesizes with an adjusted speaking rate (edge-tts prosody `rate`, clamped ±25%) if measured duration drifts more than 15% from `target_s`. Positive rate = faster. Rate key is `voice + rate + text` so the adjusted file has a distinct cache key.
- **Whisper caption timing** — `composite_cut()` attempts Whisper transcription of each beat's `.mp3` via `captions.py`. If Whisper is installed, word-level timestamps drive the FFmpeg drawtext timing instead of proportional word-count estimates. Falls back to proportional if Whisper is not installed, transcription returns empty, or the transcript has fewer words than the beat has `on_screen_text` lines (the line→word slice arithmetic needs ≥1 word per line). The model is loaded once per process via `@lru_cache` in `captions.py` — `transcribe_audio()` runs once per beat.
- **Footage resolution** — `asset_sourcer.py` caps downloads at FHD (≤1920 px height). Pexels returns 4K files by default.
- **Wikipedia image sourcing** — `WikipediaImageSource` in `asset_sourcer.py` fetches player headshots when a player name is detected in `visual_direction`. Also fetches license metadata via the `imageinfo` API (`extmetadata`) — `license`, `license_url`, `attribution`, `safe_to_publish` are stored on the `Asset` row. Wikimedia CDN rate-limits rapid downloads; the sourcer uses a 0.5 s inter-player delay, 2 s retry on 429.
- **Credential encryption** — `Credential.token_blob` uses `Encrypted` (Fernet TypeDecorator from `api/crypto.py`). Set `CREDENTIALS_KEY` in `.env` to a Fernet key (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`). If unset, values are stored as plaintext with a warning — safe for dev, not for production.

## Commands

```bash
# First-time setup
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pip install edge-tts          # TTS (Kokoro requires Python <3.13 — skip it)
# Optional: Whisper word-level caption timing
.venv/bin/pip install -e ".[captions]"
cp .env.example .env              # fill in PEXELS_API_KEY + NVIDIA_API_KEY at minimum; set TTS_PROVIDER=edge for audio
docker compose up -d              # starts postgres:5432 + redis:6379
.venv/bin/alembic upgrade head    # run migrations (0001–0003)

# Daily dev — five terminals (Ollama only needed if not using NVIDIA NIM)
.venv/bin/uvicorn api.main:app --reload                                                      # API at :8000
DATABASE_URL=... .venv/bin/celery -A worker.celery_app worker -Q generation -c 4 -l info    # LLM tasks (I/O-bound)
DATABASE_URL=... .venv/bin/celery -A worker.celery_app worker -Q rendering --concurrency=1 -l info  # render tasks (CPU-bound)
DATABASE_URL=... .venv/bin/celery -A worker.celery_app beat -l info                         # maintenance scheduler (stuck-job reaper)
ollama serve                                                                                  # local LLM (skip if using NVIDIA)

# Tests
.venv/bin/pytest                            # 144 tests across 14 files
.venv/bin/pytest tests/test_foo.py::bar -s

# New migration after changing models.py
.venv/bin/alembic revision --autogenerate -m "describe change"
.venv/bin/alembic upgrade head
```

## Architecture

The pipeline: context entry → guide generation (LLM or script parser + enrichment) → two-tier quality evaluation → render (stock footage + Wikipedia photos + TTS + MoviePy) → review → publish.

**Core principle:** everything slow runs as a Celery background job. Nothing slow happens inside a request. The browser gets HTML fragments (Jinja2) and polls for updates via HTMX — no JSON to the browser, no React.

**Job pipeline:** `POST /api/reels` creates `Reel` + `Cut` rows + an enrich `Job` row → enqueues `enrich_context` → returns an HTML fragment that polls `GET /api/reels/{id}/active-job-fragment` every 2 s → each worker updates `job.status` + `job.progress` + `job.heartbeat_at` → the fragment follows the enrich → generate job chain without a URL change.

**Context enrichment stage:** `POST /api/reels` does not enqueue generation directly. It creates the `Reel` + `Cut` rows and an `enrich` job, and enqueues `enrich_context`. That task scores the raw context with `evaluate_context()` (5 axes × 20 pts), calls `llm_enrich()` when the score is below 60 — skipped entirely for structured scripts, where enrichment would cause topic drift — stores the result on `reel.enriched_context`, transitions the reel to `generating`, then creates and enqueues the `generate_guide` job. `generate_guide` reads `reel.enriched_context or reel.context`.

**State machines** (`api/state.py`) — `REEL_TRANSITIONS` and `CUT_TRANSITIONS` dicts are the single source of truth. Always call `transition(obj, new_status, map)` — it raises `ValueError` on invalid moves. Never set `.status` directly.

**Guide generation — two paths:**

1. **Structured-script path** (~60–120 s with enrichment) — when `job.meta["generation_path"]` is `"structured"` or auto-detection finds ≥3 ALL-CAPS section headers, `script_parser.py` extracts beats directly. Then:
   - `_enrich_with_insight()` appends one tactical insight sentence to every shallow player beat, batched via the enrichment LLM. **Topic fence**: "Do not introduce matches, tournaments, scorelines, or players not mentioned in the beat text."
   - `_make_conflict_stub()` injects a conflict/weakness beat before the CTA if no body beat contains tension language. **Topic fence**: constrained to players/events in the original context only.
   - LLM is then asked for `visual_direction` descriptions only — `build_visuals_messages()` system prompt anchors the LLM to per-beat VO content; it must not infer visuals from the global context.

2. **Standard LLM path** (2–5 min) — full `MasterGuide` JSON generated from `prompt.py`. Up to 3 retries with **closed-loop feedback**: failed-guide issues from `score_guide()` + `judge_guide()` are appended as a second user message in the next attempt so the LLM knows what to fix. Best-of-3 is accepted if no attempt clears the 80/100 threshold.

The generation path is explicitly selectable via the form's "Generation path" dropdown (`auto` / `structured` / `standard`). The choice is stored in `job.meta["generation_path"]` when the job is created, and read by the task. `job.meta["path"]` records which path was actually taken, and `job.meta["stub_count"]` the number of parsed beats.

In both paths, `postprocess.py` strips section-label prefixes from `vo_script` and derives `on_screen_text` from the vo content before the guide is saved.

**Two-tier quality evaluation:**
- `score_guide()` — deterministic 17-axis rule scorer (retention architecture, narrative quality, context coverage, insight density, visual alignment, clip availability, editability, emotional impact+distribution, audio delivery+sentence length, visual variety, duration fit, caption/hashtag, CTA quality, conversational tone, hook-CTA throughline, per-beat specificity, repetition)
- `judge_guide()` — LLM semantic judge via enrichment provider (factual accuracy, expertise depth, natural speech, hallucination risk, shareability) — only called when rule score ≥ 55.
- `combined = int(rule * 0.4 + llm * 0.6)` — threshold **80 for NVIDIA generation, 65 for local Ollama** (`QUALITY_THRESHOLD_LOCAL`). Best-of-3 accepted below threshold.
- `job.meta["quality_score"]` stores the combined score on success. `job.error` is `None` on success, set to exception message on failure only.
- Feedback passed to LLM retries strips `"Score breakdown — ..."` lines — the LLM doesn't understand internal axis notation.
- Structured path has a quality gate: if score < threshold, falls through to standard LLM path. Records `structured_score` and `structured_fallback: True` in `job.meta`.

**Reliability features:**
- `task_acks_late=True` — broker acks only after task returns; a killed worker requeues rather than silently loses.
- `task_reject_on_worker_lost=True` — task requeued when worker is SIGKILLed.
- **Transient-failure retry** — `generate_guide` and `render_cut` retry up to twice with 30 s/60 s backoff when `worker/tasks/common.py::should_retry()` classifies the exception as transient (httpx transport errors, timeouts, HTTP 429/5xx). Deterministic failures — bad LLM JSON, quality-below-threshold, a missing row, ffmpeg's non-zero exit — fail once, unchanged. The retry branch resets `job.status` to `pending` before calling `self.retry()`: the idempotency guard rejects `running`, so a retry that left the status alone would be a silent no-op. It must **not** bump `job.attempts` — task entry already does. `enrich_context` stays `max_retries=0` on purpose.
- **Idempotency guard** at task entry: tasks with `status in (done, running)` return immediately (redelivery no-op).
- **Heartbeat** — tasks write `job.heartbeat_at` at every milestone. `reap_stuck_jobs` (Celery beat, every 60 s) fails any `running` job without a heartbeat update in the last 5 minutes, **and** any `pending` job whose `updated_at` is older than 30 minutes (broker was down when `.delay()` ran, or no worker consumes the queue). The pending branch keys on `updated_at`, not `created_at`, so a job sitting in retry backoff is not reaped for being old. Both roll back the owning reel (`enriching` or `generating`) / cut.
- **Atomic MP4 write** — FFmpeg writes to `.tmp.mp4`, then `os.replace()` to the final path; a killed process never leaves a servable half-written file.

## Module layout

```
api/
  main.py             FastAPI app factory; static files, Jinja2, all routers
  config.py           pydantic-settings Settings; includes nvidia_api_key, nvidia_generation_model, use_nvidia_for_generation, pexels_api_key, huggingface_api_key, huggingface_image_model, huggingface_video_model, pixabay_api_key, credentials_key
  crypto.py           Fernet seal/open_ + Encrypted SQLAlchemy TypeDecorator
  db.py               SQLAlchemy engine, SessionLocal, get_db() dependency
  models.py           All ORM models + enums; includes StageEvent
  schemas.py          Pydantic schemas for JSON endpoints
  state.py            REEL_TRANSITIONS, CUT_TRANSITIONS, transition()
  routers/
    reels.py          POST /api/reels (accepts generation_path), GET /api/reels/{id}
    jobs.py           GET /api/jobs/{id} (JSON + HTML fragment)
    cuts.py           POST /render, PATCH, POST /approve, render-status, video stream

worker/
  celery_app.py       Celery instance; acks_late=True, beat schedule, split queues
  tasks/
    common.py         should_retry() / is_transient_error() / heartbeat() — shared task helpers
    generate.py       generate_guide(job_id) — idempotency guard, heartbeat, enrichment,
                      conflict injection, visuals LLM, closed-loop eval retry, observability
    render.py         render_cut(job_id) — idempotency guard, heartbeat, resolve_or_reuse,
                      synth_to_budget, TTS-accurate timecodes, atomic MP4, observability
    maintenance.py    reap_stuck_jobs() — Celery beat task; fails stale running + pending jobs

engine/
  observability.py    record_stage() context manager — writes StageEvent rows on exit
  generation/
    guide_schema.py   Beat, PlatformGuide, MasterGuide Pydantic models
    llm.py            LLMProvider + OllamaProvider; get_llm_provider(), get_enrichment_provider(),
                      is_nvidia_generation() — True when main LLM routes to NVIDIA NIM
    prompt.py         build_messages(prior_feedback=) + build_visuals_messages() — visuals system prompt anchors LLM to per-beat VO only
    script_parser.py  BeatStub + parse() + is_structured() — structured-script extractor
    visual_fallback.py  Fallback visual_direction synthesis — section/VO keyword tables
    beat_enrichment.py  Tactical insight enrichment + conflict-beat synthesis (topic-fenced)
    evaluator.py      score_guide() — 17-axis rule scorer (0–100); see docs/evaluation.md
    llm_judge.py      judge_guide() — LLM semantic judge; 5 dims × 0–20 = 100 pts
    postprocess.py    clean_guide() — strips label prefixes; derives up to 5 on_screen_text segments
  render/
    asset_sourcer.py  PexelsVideoSource + WikipediaImageSource (with license metadata fetch);
                      HuggingFaceImageSource (FLUX.1-schnell) + HuggingFaceVideoSource (LTX-Video);
                      fallback chain: Wikipedia → Pexels → HF Video → HF Image → None;
                      resolve_or_reuse() — pins assets by beat_index + fingerprint, reuses on re-render;
                      resolve_beat_assets() still available for non-pinned use
    tts.py            EdgeTTSProvider.synthesize() + .synth_to_budget(); _audio_duration() helper
    captions.py       transcribe_audio() — Whisper word-level timestamps; no-op if not installed
    compositor.py     composite_cut() — MoviePy stage + FFmpeg drawtext stage;
                      120ms audio fade in/out per beat (moviepy.audio.fx.AudioFadeIn/AudioFadeOut via .with_effects()) for smooth narration transitions;
                      _build_text_filter() uses Whisper timestamps when available, proportional fallback;
                      atomic final write via os.replace()

migrations/
  versions/
    0001_initial.py   Original schema
    0002_improvements.py  Job heartbeat/meta, CutAsset pinning, Asset licensing, StageEvent table

tests/
  test_evaluator.py           29 tests — all 17 evaluator axes + helpers, multi-platform dedupe
  test_script_parser.py       11 tests — parse() routing, beat splitting, _derive_on_screen
  test_state.py               11 tests — REEL_TRANSITIONS, CUT_TRANSITIONS, invalid moves
  test_enrichment.py          15 tests — coerce_beat_type, _enrich_batch response parsing, topic fence
  test_audio_text_sync.py     11 tests — clean_guide() regeneration, _build_text_filter() proportional timing + whisper fallback, PATCH re-derivation, visual direction anchoring
  test_context_enricher.py    13 tests — evaluate_context axes, llm_enrich
  test_enrich_context_task.py 10 tests — idempotency, enrichment guard, structured script detection, missing reel
  test_maintenance.py          7 tests — reaper: stale running jobs, never-picked-up pending jobs, owner rollback, updated_at keying
  test_tts.py                  8 tests — provider selection, unknown-provider fallback, SilentProvider shared file, synth_to_budget clamp
  test_common.py              16 tests — transient-error classification, retry budget
  test_generate_task.py        3 tests — missing reel, transient retry, deterministic failure
  test_render_task.py          3 tests — missing cut, transient retry, success clears stale error
  test_asset_sourcer.py        4 tests — resolve_or_reuse pin, reuse, re-pin, beat isolation
  test_llm_judge.py            3 tests — neutral-score fallback on raise, garbage, out-of-range

ui/templates/
  index.html          Context-entry form; target_length + voiceover_mode + generation_path selects
  reel.html           Page shell — loops cuts, includes cut_card.html
  fragments/
    cut_card.html     Full cut card; read-only or editable (in_review)
    render_status.html  Polling fragment; video + Approve/Re-render when done
ui/static/main.css    Styles: badges, progress bar shimmer, beat table, edit fields

docs/
  architecture.md     System diagram, module breakdown, render pipeline
  data-model.md       All tables, state machines, guide JSON schema
  api.md              All HTTP endpoints, request/response shapes
  evaluation.md       Two-tier quality scoring, threshold/retry, tuning
  roadmap.md          Phase-by-phase plan with open items
```

## Data model

- `reels` — master concept; `status` tracks generation phase
- `cuts` — one row per platform; holds `guide` (JSONB), `caption`, `hashtags`, `video_path`, `thumbnail_path`
- `assets` — cached media files; deduplicated by `(source, source_ref)`; `source` is `pexels`, `wikipedia`, `huggingface`, or `huggingface_video`; `type` is `footage` or `photo`; includes `license_url`, `attribution`, `safe_to_publish`. HF-generated assets are `safe_to_publish=True`.
- `cut_assets` — per-beat asset binding ledger; `beat_index` + `order_in_beat` identify position; `resolved_from` is `sha256(visual_direction)[:16]` for change detection; unique constraint on `(cut_id, beat_index, order_in_beat)`; `start_s`/`end_s` updated after TTS measurement
- `jobs` — every async operation; includes `started_at`, `heartbeat_at`, `meta` (JSON — stores `generation_path`, `path`, `stub_count`, `quality_score` on success, and optionally `structured_score`/`structured_fallback` when structured path fell back to standard); `error` is `None` on success, set to exception message on failure only
- `stage_events` — instrumentation: one row per pipeline stage (enrich, generate, judge, composite); stores `stage`, `provider`, `model_name`, `latency_ms`, `ok`, `detail`, `score`
- `credentials` — OAuth tokens; `token_blob` column is encrypted at rest via `Encrypted` TypeDecorator

Video files live on disk (`VIDEO_STORE_DIR`); Wikipedia images in `ASSET_STORE_DIR/wiki/`; TTS audio in `ASSET_STORE_DIR/tts/`; DB stores paths only.

`cuts.guide` is the full serialized `PlatformGuide` dict. Always deserialize with `PlatformGuide(**cut.guide)` before using.

## Key conventions

- **Routers return HTML, not JSON** (except `GET /api/jobs/{id}`). Use `response_class=HTMLResponse` and `templates.TemplateResponse(...)`.
- **Every Celery task** receives a `job_id`, starts with an idempotency guard, calls `heartbeat()` (from `worker/tasks/common.py`) at every milestone, and always sets final `job.status = done|failed` before returning.
- **Idempotency guard**: at task entry check `if job.status in (JobStatus.done, JobStatus.running): return`. Done = redelivery no-op. Running = live sibling (reaper handles dead ones via heartbeat).
- **Asset pinning**: use `resolve_or_reuse()` (not `resolve_beat_assets()`) from render tasks. It reuses pinned assets when `visual_direction` fingerprint matches; re-resolves and re-pins only changed beats. This makes re-renders fast and deterministic.
- **CutAsset timing**: `start_s`/`end_s` are written after TTS duration measurement (in the second loop), not during asset resolution. They reflect actual rendered timecodes.
- **Wikipedia licensing**: always check `asset.safe_to_publish` before publishing. Wikipedia images are often CC-BY-SA (requires attribution) or non-free. Pexels assets hardcode `safe_to_publish=True`.
- **TTS caching**: `EdgeTTSProvider.synthesize(text, rate="+0%")` is idempotent. Cache key includes voice + rate + normalized text. `synth_to_budget()` may produce a second file at an adjusted rate. Clearing `ASSET_STORE_DIR/tts/` forces re-synthesis.
- **Observability**: wrap slow/paid call sites with `record_stage(db, reel_id, "stage_name")`. The context manager writes a `StageEvent` row on exit (success or failure). Do not instrument trivial DB operations.
- **`heartbeat()` lives in `worker/tasks/common.py`** — never redefine it per task file. Three copies previously drifted apart; the reaper depends on all tasks writing `heartbeat_at` the same way.
- **Never skip the state machine**: use `transition(obj, new_status, MAP)` for all status changes.
- **Beat types are coerced**: `guide_schema.py` maps unknown strings ("closing", "outro", "tactical_analysis") to "cta" or "body". Safe to add new aliases.
- **on_screen_text**: `_derive_on_screen()` generates up to 5 segments (one per sentence, 7 words each). `clean_guide()` preserves all 5 (`deduped[:5]`). The PATCH endpoint also preserves 5 (`lines[:5]`). The compositor reads `[:5]` in `_build_text_filter()`.
- **Closed-loop eval retry**: `build_messages()` accepts `prior_feedback: list[str] | None`. Pass `last_issues` from the previous attempt. The LLM receives the issues as a second user message. Best-of-3 is returned if nothing clears the 80/100 threshold — do not raise an error when a valid guide exists.
- **Enrichment provider**: always use `get_enrichment_provider()` (not `get_llm_provider()`) for enrichment, conflict generation, and judging. It auto-routes to NVIDIA NIM when `NVIDIA_API_KEY` is set.
- **Atomic file writes**: compositor writes FFmpeg output to `.tmp.mp4` then `os.replace()`. Never stream-write the final video path — a killed process must not leave a servable partial file. All asset sourcer downloads (Pexels, Wikipedia, both HuggingFace sources) go through `_atomic_write()` or a streamed `.tmp` + `os.replace()`. This is not optional: every sourcer caches by `if path.exists()`, so a truncated file is reused on every later render.
- **Celery workers don't auto-reload**: always `pkill -f "celery.*worker"` and restart after code changes. The API server (`--reload`) hot-reloads but workers do not.
- **`score_guide()` signature**: takes `(context: str, guide: MasterGuide, target_length_s: float)` — requires a full `MasterGuide`, not a `PlatformGuide`. Wrap with `MasterGuide(title="", niche="", cuts=[pg])` when scoring a single platform guide in scripts/tests. Beat-level axes de-duplicate beats across `guide.cuts` by `(index, vo_script, visual_direction)` — both platform guides normally hold identical beats. Per-cut axes (duration fit, caption, hashtags) still deduct once per platform, by design.
- **Beat field is `b.type`** (not `b.beat_type`) — the Pydantic `Beat` model uses `type` as the field name.
- **`is_nvidia_generation()`** in `llm.py` — returns `True` when main LLM routes to NVIDIA. Used by `generate_guide` to select quality threshold and label StageEvents correctly.
- **`_generate_caption_hashtags()`** in `generate.py` — structured path generates captions/hashtags via enrichment LLM from actual VO content; falls back to hardcoded template on failure.
- **Evaluator thresholds**: `MAX_WPS=4.0` (body/CTA beats), `MAX_WPS_HOOK=3.0` (hook beats — tighter cap; hook violation costs 4 pts vs 2 pts for body; Axis 9 max deduction 10 pts total), `MAX_OPENER_WORDS=16`. Narrative insight tactical sub-axis gives 2/4 baseline to non-tactical reels so they aren't penalised for missing football jargon.
- **Structured script enrichment guard**: `enrich_context.py` imports `script_parser.is_structured()` (≥3 labelled sections — the exact test `parse()` applies). Single source of truth: never add a second header regex, or the guard and the parser will disagree about the same input. When True, `llm_enrich()` is skipped even if score < 60 — the script's topic is already locked in; enrichment would cause context drift. `job.meta["enrich_skipped"]` records the reason (`"structured_script"` or `"score_above_threshold"`).
- **Audio fade in compositor**: `composite_cut()` applies `.with_effects([AudioFadeIn(0.12), AudioFadeOut(0.12)])` to each beat's `AudioFileClip`. Order: subclip if too long → fade → `.with_start(t)`. Fade before `with_start` is required — fades compute against the clip's own timeline, not the composite. `AudioFileClip` has no `.audio_fadein()`/`.audio_fadeout()` methods in MoviePy 2.x — fades are effects, not chainable methods. The exception from calling the non-existent methods was caught by a bare `except Exception: pass` around the VO track builder, so this shipped for months rendering every video with zero audio before a live end-to-end run caught it; the handler now logs via `_log.exception()` instead of swallowing silently.
- **Topic fence in enrichment prompts**: `_enrich_batch()` and `_make_conflict_stub()` include an explicit "Do not introduce matches, tournaments, scorelines, or players not mentioned in the beat/context" constraint. Prevents LLM from drifting into unrelated events.
- **Visual direction prompt anchoring**: `build_visuals_messages()` system prompt instructs the LLM to derive `visual_direction` ONLY from the specific players, actions, and events named in that beat's VO — not from the global context.
- **Video streaming path guard**: `stream_video` validates `cut.video_path` is under `VIDEO_STORE_DIR` via `Path.resolve().is_relative_to()` before serving — never bypass this.

## Build phase status

- **Phase 0** ✅ — Foundations: async job pipeline, DB schema, state machine, no-op task
- **Phase 1** ✅ — Generation: LLM guide, context-entry UI, structured-script parser, two-tier quality evaluator, enrichment + conflict injection; closed-loop eval retry with feedback; best-of-3 acceptance; explicit generation path selection
- **Phase 2** ✅ — Render: Pexels footage + Wikipedia player photos (with license metadata), Edge TTS with rate-budget control, MoviePy compositor with Ken Burns, Whisper word-level timing (with proportional fallback), atomic MP4 writes
- **Phase 3** ✅ — Review/edit loop: editable beats, editable caption/hashtags, approve
- **Phase 3.5** ✅ — Hardening: acks_late reliability, idempotency guards, heartbeat + stuck-job reaper, per-beat asset pinning (deterministic re-render), observability (StageEvent), credential encryption, a substantially expanded test suite; evaluator upgraded to 17 axes (conversational tone, hook-CTA throughline, per-beat specificity, repetition)
- **Phase 4** 🔲 — Publishing: YouTube Data API + Instagram Graph API; safe_to_publish gate; attribution block in captions
- **Phase 5** 🔲 — Analytics: post-publish metrics pull-back, cost-per-reel reporting from stage_events, music mixing

## Worker queues

- **`generation` queue** — `generate_guide`, `enrich_context`. I/O-bound. Run with `--concurrency=4`.
- **`rendering` queue** — `render_cut`. CPU-bound. Run with `--concurrency=1`. Restarts after 10 tasks (`worker_max_tasks_per_child=10`) to prevent ffmpeg handle leaks.
- **Celery beat** — runs `reap_stuck_jobs` every 60 s. Start with `celery -A worker.celery_app beat`.

## What is not yet wired in

- `music_cue` — generated by the LLM but no music file is fetched or mixed into renders. Intended approach: FFmpeg `amix` + `agate` sidechain for auto-ducking under VO.
- `credentials` table — schema + encryption exist; no OAuth flow yet (Phase 4)
- `scheduled` / `publishing` / `published` cut statuses — state machine entries only (Phase 4)
- Insight enrichment for the **standard LLM path** — currently only applied to structured-script beats
- Multi-image collage within a single beat — currently cycles sequentially; no side-by-side layout
- Cost calculation in `StageEvent.cost_usd` — `latency_ms` and `tokens_in/out` fields exist but pricing lookup is not implemented
- `PIXABAY_API_KEY` (`pixabay_api_key` in config) — field exists for a planned Pixabay music/video source; no `PixabayProvider` implemented yet

## Docs

- `docs/architecture.md` — system diagram, module breakdown, render pipeline detail
- `docs/data-model.md` — all tables, state machines, guide JSON schema
- `docs/api.md` — all HTTP endpoints, request/response shapes
- `docs/evaluation.md` — two-tier quality scoring, threshold/retry, tuning guide
- `docs/roadmap.md` — phase-by-phase plan with open items and sequencing rationale
