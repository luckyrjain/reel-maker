# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Stack

All-Python: FastAPI + Celery/Redis + PostgreSQL + SQLAlchemy/Alembic + MoviePy/FFmpeg + Pillow. No Node. UI is server-rendered Jinja2 + HTMX (no SPA, no build step).

## Runtime requirements

- **Starlette ≥ 0.36 / 1.x** — `TemplateResponse` takes `request` as the first positional argument, not inside the context dict. All template calls use `templates.TemplateResponse(request, "template.html", {...})`.
- **LLM — two tiers:**
  - `LLM_MODEL` (main generation, default `qwen3:14b`) — used for full guide generation and visuals prompts. Any OpenAI-compatible model works. The 3.2B `llama3.2` model is too small — it fails schema validation. Test a new model with one generation before relying on it.
  - `LLM_ENRICHMENT_MODEL` (enrichment + judge, default `qwen3:14b`) — used for `_enrich_with_insight()`, `_make_conflict_stub()`, and the LLM quality judge. Requires a capable model that returns reliable JSON arrays. If `NVIDIA_API_KEY` is set, enrichment is automatically routed to `NVIDIA_ENRICHMENT_MODEL` (default `nvidia/nemotron-3-super-120b-a12b`) at NVIDIA NIM instead.
  - Set `USE_NVIDIA_FOR_GENERATION=true` in `.env` to route main guide generation to NVIDIA NIM (`NVIDIA_GENERATION_MODEL`, default `nvidia/nemotron-3-super-120b-a12b`). Produces significantly higher quality (81/100 vs 50/100 measured). Uses `is_nvidia_generation()` helper in `llm.py` to select provider and adaptive threshold.
- **LLM timeout** — `OllamaProvider` uses a 360 s HTTP timeout (`llm.py`). Smaller local models can take 3–5 min per call.
- **NVIDIA NIM** — set `NVIDIA_API_KEY` in `.env` to route enrichment, conflict-beat generation, and LLM judge calls to `https://integrate.api.nvidia.com/v1`. The `NVIDIA_ENRICHMENT_MODEL` defaults to `nvidia/nemotron-3-super-120b-a12b`. Leave blank to use local Ollama.
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
.venv/bin/pytest                            # 323 tests across 30+ files (4 test_compositor tests need ffmpeg on PATH)
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
- **Transient-failure retry** — `generate_guide`, `render_cut` and `publish_cut` retry up to twice with 30 s/60 s backoff when `worker/tasks/common.py::should_retry()` classifies the exception as transient (httpx transport errors, timeouts, HTTP 429/5xx). Deterministic failures — bad LLM JSON, quality-below-threshold, a missing row, ffmpeg's non-zero exit — fail once, unchanged. The retry branch resets `job.status` to `pending` before calling `self.retry()`: the idempotency guard rejects `running`, so a retry that left the status alone would be a silent no-op. It must **not** bump `job.attempts` — task entry already does. `enrich_context` stays `max_retries=0` on purpose. All of this lives in `worker/tasks/common.py::job_task` — never re-implement it in a task.
- **Idempotency guard** at task entry: tasks with `status in (done, running)` return immediately (redelivery no-op).
- **Heartbeat** — tasks write `job.heartbeat_at` at every milestone. `reap_stuck_jobs` (Celery beat, every 60 s) fails any `running` job without a heartbeat update in the last 5 minutes, **and** any `pending` job whose `updated_at` is older than 30 minutes (broker was down when `.delay()` ran, or no worker consumes the queue). The pending branch keys on `updated_at`, not `created_at`, so a job sitting in retry backoff is not reaped for being old. Both roll back the owning reel (`enriching` or `generating`) / cut.
- **Atomic MP4 write** — FFmpeg writes to `.tmp.mp4`, then `os.replace()` to the final path; a killed process never leaves a servable half-written file.

## Module layout

```
api/
  main.py             FastAPI app factory; static files, Jinja2, all routers
  config.py           pydantic-settings Settings; includes nvidia_api_key, nvidia_generation_model, use_nvidia_for_generation, pexels_api_key, huggingface_api_key, huggingface_image_model, huggingface_video_model, pixabay_api_key, credentials_key, max_paid_llm_calls_per_reel, nvidia_price_per_1m_input_tokens, nvidia_price_per_1m_output_tokens, public_base_url, youtube_oauth_client_id/secret, meta_oauth_app_id/secret, music_library_dir, huggingface_price_per_image, huggingface_price_per_video_second
  crypto.py           Fernet seal/open_ + Encrypted SQLAlchemy TypeDecorator
  db.py               SQLAlchemy engine, SessionLocal, get_db() dependency
  models.py           All ORM models + enums; includes StageEvent
  oauth.py            Generic OAuth2 authorization-code flow — YouTubeOAuth, InstagramOAuth,
                      new_state()/consume_state() (process-local CSRF state, single-operator tool)
  schemas.py          Pydantic schemas for JSON endpoints
  state.py            REEL_TRANSITIONS, CUT_TRANSITIONS, transition()
  routers/
    reels.py          POST /api/reels (accepts generation_path + platforms), GET /api/reels (list),
                      GET /api/reels/{id} (pipeline cost/latency/quality panel), POST /api/reels/estimate
    jobs.py           GET /api/jobs/{id} (JSON + HTML fragment)
    cuts.py           POST /render, PATCH, POST /approve, POST /publish, render-status,
                      publish-status, video stream
    credentials.py    GET /api/credentials (connect/disconnect UI), GET /{provider}/authorize,
                      GET /{provider}/callback, POST /{provider}/disconnect

worker/
  celery_app.py       Celery instance; acks_late=True, beat schedule, split queues
  tasks/
    common.py         job_task() — the shared Job lifecycle decorator (idempotency guard, running-stamp, atomic
                      done-stamp, transient-retry reset, failure stamp + per-job-type owner rollback via
                      api/state.py::JOB_IN_FLIGHT); rollback_owner(); should_retry() / is_transient_error() /
                      heartbeat() — shared task helpers
    generate.py       generate_guide(job_id) — idempotency guard, heartbeat, enrichment,
                      conflict injection, visuals LLM, closed-loop eval retry, observability,
                      paid-call budget cap (_enforce_paid_call_budget)
    render.py         render_cut(job_id) — idempotency guard, heartbeat, resolve_or_reuse,
                      synth_to_budget, TTS-accurate timecodes, atomic MP4, observability;
                      resolves a music track via get_music_sourcer().find(music_cue) and
                      passes it to composite_cut()
    publish.py        publish_cut(job_id) — idempotency guard, heartbeat, safe_to_publish gate,
                      builds caption via build_published_caption() (appends attribution block),
                      dispatches to engine/publish/registry.py, records platform_post_id
    maintenance.py    reap_stuck_jobs() — Celery beat task; fails stale running + pending jobs
                      (rolls back cuts stuck in "rendering" OR "publishing")
    metrics.py        pull_publish_metrics() — Celery beat task (every 6h); pulls views/likes/
                      comments for published cuts via get_metrics_fetcher(), skips platforms/
                      credentials without a fetcher, commits per-cut, tolerates per-cut failures

engine/
  observability.py    record_stage() context manager — writes StageEvent rows on exit;
                      paid_call_count() — counts nvidia-provider StageEvents for a reel (budget cap)
  generation/
    guide_schema.py   Beat, PlatformGuide, MasterGuide Pydantic models — platform Literal includes tiktok
    llm.py            LLMProvider + OllamaProvider (captures last_usage/total_usage token counts);
                      get_llm_provider(), get_enrichment_provider(), is_nvidia_generation()
    pricing.py        llm_cost_usd() — NVIDIA per-token cost from Settings rates (0 until configured)
    estimate.py       estimate_generation() — pre-generation call-count/time/cost estimate for the
                      create-reel form, cost sourced from this operator's own StageEvent history
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
                      resolve_beat_assets() still available for non-pinned use; records "asset_hf_video"/
                      "asset_hf_image" StageEvents (cost_usd only on an actual generation call, not a
                      cache hit — tracked via last_call_was_generated); LocalMusicSource.find(music_cue) —
                      keyword-overlap match against Settings.music_library_dir, no external API
    pricing.py        hf_image_cost_usd() / hf_video_cost_usd(duration_s) — config-driven HF
                      asset-generation cost, mirrors engine/generation/pricing.py's honesty policy
                      (0.0 until the operator sets a real per-unit price)
    tts.py            EdgeTTSProvider.synthesize() + .synth_to_budget(); _audio_duration() helper
    captions.py       transcribe_audio() — Whisper word-level timestamps; no-op if not installed
    compositor.py     composite_cut() — MoviePy stage + FFmpeg drawtext stage;
                      120ms audio fade in/out per beat (moviepy.audio.fx.AudioFadeIn/AudioFadeOut via .with_effects()) for smooth narration transitions;
                      _build_text_filter() uses Whisper timestamps when available, proportional fallback;
                      accepts music_path — _build_ffmpeg_args() adds a sidechain-ducked (VO present) or
                      plain-volume (no VO) music mix, always atrim'd to the render's total_duration;
                      atomic final write via os.replace()
  publish/
    base.py           Publisher interface + PublishResult dataclass; publish() takes an explicit
                      caption: str param (not cut.caption) so attribution text can be injected
                      without polluting the DB-stored caption
    registry.py       get_publisher(platform) / credential_provider_for_platform(platform) /
                      get_metrics_fetcher(platform) — the last returns None (not a raise) for an
                      unmapped platform (e.g. tiktok)
    gate.py           assert_safe_to_publish() / unsafe_assets() — enforces Asset.safe_to_publish
                      before any publish call; the one place that field is actually checked
    attribution.py    build_attribution_block() — formats Wikipedia asset attribution/license into
                      a caption suffix; build_published_caption() appends it to cut.caption without
                      mutating the DB-stored value
    metrics.py        EngagementMetrics dataclass + MetricsFetcher base (fetch() takes db, not just
                      cut+credential, so a fetcher can refresh+persist an expired token);
                      YouTubeMetricsFetcher (videos.list?part=statistics — stable, high confidence;
                      refreshes via youtube.py::get_valid_access_token() before every call, since
                      pull_publish_metrics runs every 6h and Google tokens expire in ~1h);
                      InstagramMetricsFetcher (Graph API /insights?metric=plays,likes,comments —
                      lower confidence, Meta has renamed Reels Insights metrics before); both use
                      Authorization: Bearer headers, never a token in params (leaks into
                      httpx.HTTPStatusError.__str__() → job.error/logs)
    youtube.py        YouTubePublisher — resumable upload (init POST + PUT); get_valid_access_token()
                      (module-level, shared with YouTubeMetricsFetcher) refreshes an expired access
                      token via the stored refresh_token first
    instagram.py      InstagramPublisher — container create/poll/publish (Reels). Requires
                      settings.public_base_url to be a real public HTTPS URL — Instagram
                      fetches the video itself, it does not accept an upload body
    tiktok.py         TikTokPublisher — deliberately raises NotImplementedError; the Content
                      Posting API needs a separate audited app review, unlike YouTube/Instagram

migrations/
  versions/
    0001_initial.py   Original schema
    0002_improvements.py  Job heartbeat/meta, CutAsset pinning, Asset licensing, StageEvent table
    0003_context_enrichment.py  enriched_context column; enriching/enrich enum values
    0004_publishing.py  tiktok CutPlatform value; Credential.provider_account_id + refresh_token_blob
    0005_metrics.py   Cut.views/likes/comments/metrics_updated_at columns

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
  test_job_lifecycle.py       24 tests — job_task on dummy tasks (in-memory SQLite): guard, atomic done-stamp, prepare-before-stamp, retry/backoff, failure stamp, per-job-type owner rollback, after_commit, `.delay` signature regression, JOB_IN_FLIGHT table
  test_generate_task.py        6 tests — missing reel, transient retry, deterministic failure, structured-path music_cue default
  test_render_task.py          5 tests — missing cut, transient retry, success clears stale error, music wiring
  test_asset_sourcer.py        4 tests — resolve_or_reuse pin, reuse, re-pin, beat isolation
  test_asset_sourcer_cost.py   9 tests — HF cost StageEvents charged only on real generation, not cache hits
  test_music_source.py         8 tests — LocalMusicSource keyword matching, missing/empty library
  test_llm_judge.py            3 tests — neutral-score fallback on raise, garbage, out-of-range
  test_reels_router.py        17 tests — reel list/detail routes, platform-selection form, pipeline panel, htmx id/target consistency, quality/views columns, published-cut engagement stats
  test_pricing.py              4 tests — llm_cost_usd() rate application, zero-rate default
  test_llm_provider.py         4 tests — OllamaProvider last_usage/total_usage capture
  test_estimate.py             8 tests — generation path resolution, historical cost averaging incl. structured-fallback exclusion
  test_observability.py        3 tests — paid_call_count() scoping and filtering
  test_publish_gate.py         3 tests — safe_to_publish enforcement
  test_oauth.py               13 tests — OAuth state CSRF, YouTube/Instagram authorize+exchange, long-lived token swap, token-in-header regression
  test_credentials_router.py   9 tests — connect/callback/disconnect routes
  test_publish_task.py         7 tests — publish_cut idempotency, budget/safety gates, transient vs deterministic failure, attribution caption
  test_publish_registry.py     7 tests — platform→publisher, platform→credential-provider, platform→metrics-fetcher mapping
  test_cuts_publish_router.py  6 tests — POST /cuts/{id}/publish state-guard and enqueue
  test_youtube_publisher.py    6 tests — resumable upload flow, token refresh, whitespace-caption fallback
  test_instagram_publisher.py  7 tests — container create/poll/publish flow, error paths, token-in-header regression
  test_attribution.py          8 tests — build_attribution_block dedup/formatting, build_published_caption
  test_metrics_fetcher.py      6 tests — YouTube/Instagram metrics parsing, token-in-header regression
  test_metrics_task.py         6 tests — pull_publish_metrics fetcher/credential skip paths, per-cut failure isolation
  test_compositor.py           7 tests — _build_ffmpeg_args no-music/sidechain/no-VO branches, real ffmpeg music-mixing end-to-end

ui/templates/
  index.html          Context-entry form; niche/platform picker + target_length + voiceover_mode +
                      generation_path selects; live cost/time estimate (htmx → cost_estimate.html)
  reels_list.html     Paginated reel list (GET /api/reels) — status badges, per-cut platform badges,
                      Quality (latest job's quality_score) and Views (max across the reel's cuts) columns
  reel.html           Page shell — pipeline cost/latency/quality panel, loops cuts, includes cut_card.html
  credentials.html    Connected-accounts page — connect/disconnect per provider, TikTok shown as
                      not-yet-available
  fragments/
    cut_card.html     Full cut card; read-only or editable (in_review); render/approve/publish actions
                      per CutStatus branch; published branch shows views/likes/comments once
                      metrics_updated_at is set, else a "checked every 6h" hint
    render_status.html   Polling fragment; video + Approve/Re-render when done
    publish_status.html  Polling fragment (id="publish-status-{cut.id}", distinct from its parent
                      "publish-section-{cut.id}" target — do not reuse the parent's id, that
                      creates nested duplicate DOM ids on swap)
    cost_estimate.html   Pre-generation estimate fragment (POST /api/reels/estimate)
ui/static/main.css    Styles: badges (Job + Reel/Cut status enums both), progress bar shimmer,
                      beat table, edit fields, pipeline panel, credential cards, platform picker,
                      engagement-stats (post-publish views/likes/comments)

docs/
  architecture.md     System diagram, module breakdown, render pipeline
  data-model.md       All tables, state machines, guide JSON schema
  api.md              All HTTP endpoints, request/response shapes
  evaluation.md       Two-tier quality scoring, threshold/retry, tuning
  roadmap.md          Phase-by-phase plan with open items
```

## Data model

- `reels` — master concept; `status` tracks generation phase
- `cuts` — one row per platform (`youtube_shorts`, `instagram_reels`, or `tiktok`); holds `guide` (JSONB), `caption`, `hashtags`, `video_path`, `thumbnail_path`, `platform_post_id`, `published_at`, `views`, `likes`, `comments`, `metrics_updated_at` (last three populated by `pull_publish_metrics()`, `None` until the first successful pull)
- `assets` — cached media files; deduplicated by `(source, source_ref)`; `source` is `pexels`, `wikipedia`, `huggingface`, or `huggingface_video`; `type` is `footage` or `photo`; includes `license_url`, `attribution`, `safe_to_publish`. HF-generated assets are `safe_to_publish=True`.
- `cut_assets` — per-beat asset binding ledger; `beat_index` + `order_in_beat` identify position; `resolved_from` is `sha256(visual_direction)[:16]` for change detection; unique constraint on `(cut_id, beat_index, order_in_beat)`; `start_s`/`end_s` updated after TTS measurement
- `jobs` — every async operation (`enrich`, `generate`, `render`, `publish`); includes `started_at`, `heartbeat_at`, `meta` (JSON — stores `generation_path`, `path`, `stub_count`, `quality_score` on success, and optionally `structured_score`/`structured_fallback` when structured path fell back to standard); `error` is `None` on success, set to exception message on failure only
- `stage_events` — instrumentation: one row per pipeline stage (enrich, generate, judge, visuals, enrich_conflict, caption_hashtags, context_enrich, composite, publish); stores `stage`, `provider`, `model_name`, `latency_ms`, `tokens_in`, `tokens_out`, `cost_usd`, `ok`, `detail`, `score`. `provider == "nvidia"` StageEvents are what `paid_call_count()` counts toward the budget cap.
- `credentials` — OAuth tokens for publish-target accounts; `token_blob` and `refresh_token_blob` are encrypted at rest via `Encrypted` TypeDecorator (auto-decrypted on read — code sees plain strings); `provider_account_id` holds a provider-specific ID discovered during OAuth (e.g. the Instagram Business Account ID behind a connected Facebook Page); `provider` is `"youtube"` or `"instagram"` (not the `CutPlatform` value — see `credential_provider_for_platform()`)

Video files live on disk (`VIDEO_STORE_DIR`); Wikipedia images in `ASSET_STORE_DIR/wiki/`; TTS audio in `ASSET_STORE_DIR/tts/`; DB stores paths only.

`cuts.guide` is the full serialized `PlatformGuide` dict. Always deserialize with `PlatformGuide(**cut.guide)` before using.

## Key conventions

- **Routers return HTML, not JSON** (except `GET /api/jobs/{id}`). Use `response_class=HTMLResponse` and `templates.TemplateResponse(...)`.
- **Every Celery task** is `@celery_app.task(bind=True, max_retries=n)` over `@job_task("<job type>", prepare=..., after_commit=...)` from `worker/tasks/common.py`, wrapping a body `(self, db, job, ctx)`. The decorator owns the idempotency guard, running-stamp, atomic done-stamp, retry-reset and failure stamp; the body calls `heartbeat()` at every milestone and raises to fail.
  - `prepare(db, job) -> ctx` runs after the guard and **before** the running-stamp: row loads, `... no longer exists` guards and budget checks go here, so a failure there never bumps `attempts` or sets `started_at`.
  - The body must **not commit after its last domain mutation** — the done-stamp commit lands it atomically with `status = done`. `record_stage()`/`heartbeat()` commit, so keep them before the final mutations.
  - `after_commit(result)` runs after the done commit (used by `enrich_context` to enqueue `generate_guide`); it receives the body's return value, never a session, and never retries. Its failure additionally rolls back `after_commit_fail_owner`.
  - The wrapper's signature is forced to `(self, job_id)`: `functools.wraps` alone exposes the body's signature and `.delay(job_id)` raises `TypeError`.
  - Failure rolls back the owner via `api/state.py::JOB_IN_FLIGHT` — only the state that job type owns (a stale render job never flips a `publishing` cut). The reaper uses the union (`IN_FLIGHT_STATES`). `guide_ready`/`in_review` are deliberately absent.
- **Idempotency guard**: `job_task` returns at entry if `job.status in (JobStatus.done, JobStatus.running)`. Done = redelivery no-op. Running = live sibling (reaper handles dead ones via heartbeat). Check-then-stamp has no row lock — known, out of scope.
- **Asset pinning**: use `resolve_or_reuse()` (not `resolve_beat_assets()`) from render tasks. It reuses pinned assets when `visual_direction` fingerprint matches; re-resolves and re-pins only changed beats. This makes re-renders fast and deterministic.
- **CutAsset timing**: `start_s`/`end_s` are written after TTS duration measurement (in the second loop), not during asset resolution. They reflect actual rendered timecodes.
- **Wikipedia licensing**: always check `asset.safe_to_publish` before publishing. Wikipedia images are often CC-BY-SA (requires attribution) or non-free. Pexels assets hardcode `safe_to_publish=True`.
- **TTS caching**: `EdgeTTSProvider.synthesize(text, rate="+0%")` is idempotent. Cache key includes voice + rate + normalized text. `synth_to_budget()` may produce a second file at an adjusted rate. Clearing `ASSET_STORE_DIR/tts/` forces re-synthesis.
- **Observability**: wrap slow/paid call sites with `record_stage(db, reel_id, "stage_name")`. The context manager writes a `StageEvent` row on exit (success or failure). Do not instrument trivial DB operations.
- **`heartbeat()` and `job_task` live in `worker/tasks/common.py`** — never redefine either per task file. Three `heartbeat()` copies previously drifted apart, and the guard/retry/failure stanza was later copied four times; the reaper depends on all tasks writing `heartbeat_at` the same way.
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
- **safe_to_publish is enforced exactly once**: `engine/publish/gate.py::assert_safe_to_publish()`, called at the top of `publish_cut`. Nowhere else checks it — computing the field (asset_sourcer.py) is not the same as enforcing it.
- **Paid-call budget cap**: `_enforce_paid_call_budget()` in `generate.py` checks `paid_call_count(db, reel.id) < Settings.max_paid_llm_calls_per_reel` at task entry and before each standard-path retry attempt. It's a lifetime-per-reel count (no per-job scoping) — not exploitable today since nothing re-triggers a `generate` Job for a reel that already has one, but a future "regenerate guide" flow would need an explicit reset path, not just raising the global setting.
- **Cost estimates come from real history, not guessed token counts**: `engine/generation/estimate.py::estimate_generation()` averages actual `StageEvent.cost_usd` from past reels on the same generation path — reels where the structured path fell through to standard (`job.meta["structured_fallback"]`) are excluded from the "standard" bucket so they don't inflate it with wasted structured-attempt cost. Reports "no history yet" rather than fabricating a number.
- **Cut status "failed" is ambiguous by design**: it covers both a failed render and a failed publish. `CUT_TRANSITIONS["failed"]` allows both `"draft"` (retry render) and `"approved"` (retry publish) — which target applies is decided by which endpoint the operator hits (`/render` vs `/publish`), not tracked on the Cut itself.
- **HTMX fragment IDs**: a fragment returned by a trigger endpoint (`render_status.html`, `publish_status.html`) must use a **different** `id` than the stable container it gets swapped into (`render-section-{id}` / `publish-section-{id}`) — reusing the parent's id creates nested duplicate DOM ids after the `innerHTML` swap. The fragment's own polling loop then self-swaps via `outerHTML` using that distinct id.
- **`credential.token_blob` reads as plaintext**: the `Encrypted` TypeDecorator decrypts on load automatically — application code (publishers, `credentials.py`) never calls `crypto.open_()`/`seal()` directly, just assigns/reads the attribute.
- **Instagram publishing needs a public URL**: `InstagramPublisher` builds a `video_url` from `Settings.public_base_url` pointing at `GET /api/cuts/{id}/video` — Instagram's Graph API fetches the file itself rather than accepting an upload body, so this does not work behind `localhost`/NAT alone.
- **TikTok publishing is not implemented on purpose**: `TikTokPublisher.publish()` raises `NotImplementedError` — the Content Posting API requires a separate audited app review, unlike YouTube/Instagram's self-serve OAuth. TikTok is still a selectable `CutPlatform` for render/review.
- **`Publisher.publish()` takes an explicit `caption: str` param**: callers must pass the caption to send (`worker/tasks/publish.py` builds it via `build_published_caption()`), never read `cut.caption` directly inside a publisher — that's how the attribution block reaches the platform without being written back into the DB-stored caption.
- **No Pixabay Music API**: Pixabay's public REST API has never documented a Music search endpoint (only Images/Video), so `music_cue` matching is deliberately local — `LocalMusicSource` keyword-matches against files the operator drops in `Settings.music_library_dir`. `PIXABAY_API_KEY` stays unwired for the reason above, not for lack of time.
- **Music mixing is silent-by-default**: `LocalMusicSource.find()` returns `None` (no music mixed in, current behavior preserved) when the library directory is missing, empty, or nothing overlaps the cue — never an error. `_build_ffmpeg_args()` always adds `atrim=duration={total_duration}` to the looped (`-stream_loop -1`) music input; omitting it fills the render output volume with an infinite stream and previously produced a "no space left on device" ffmpeg error.
- **Metrics fetchers are best-effort**: `get_metrics_fetcher(platform)` returns `None` (not a raise) for a platform with no fetcher (e.g. tiktok) or in general — `pull_publish_metrics()` skips that cut rather than failing the whole task. A per-cut fetch exception is caught, logged, and skipped too; one bad cut never blocks metrics for the rest.
- **Token-in-URL is a logging/DB leak, not just a security nicety**: any `httpx.get(url, params={"access_token": ...})` embeds the token in `httpx.HTTPStatusError.__str__()` on a failed call, and that string can land in `job.error` (persisted, shown in the UI) or an exception log — or, for a route that surfaces the exception message directly (`api/routers/credentials.py`), in the HTTP response body itself. Every call site added in Phase 5 (and pre-existing ones fixed alongside it — `api/oauth.py::discover_account()`, `InstagramOAuth.exchange_code()`/`exchange_long_lived_token()`, `engine/publish/instagram.py::_wait_until_ready()`) puts the credential in an `Authorization: Bearer` header (bearer tokens) or a POST body (`client_secret` — RFC 6749 §3.2 requires every OAuth2 token endpoint to support POST for exactly this reason) instead of URL query params.

## Build phase status

- **Phase 0** ✅ — Foundations: async job pipeline, DB schema, state machine, no-op task
- **Phase 1** ✅ — Generation: LLM guide, context-entry UI, structured-script parser, two-tier quality evaluator, enrichment + conflict injection; closed-loop eval retry with feedback; best-of-3 acceptance; explicit generation path selection
- **Phase 2** ✅ — Render: Pexels footage + Wikipedia player photos (with license metadata), Edge TTS with rate-budget control, MoviePy compositor with Ken Burns, Whisper word-level timing (with proportional fallback), atomic MP4 writes
- **Phase 3** ✅ — Review/edit loop: editable beats, editable caption/hashtags, approve
- **Phase 3.5** ✅ — Hardening: acks_late reliability, idempotency guards, heartbeat + stuck-job reaper, per-beat asset pinning (deterministic re-render), observability (StageEvent), credential encryption, a substantially expanded test suite; evaluator upgraded to 17 axes (conversational tone, hook-CTA throughline, per-beat specificity, repetition)
- **Phase 4a** ✅ — Operator visibility: reel list page, per-reel cost/latency/quality panel sourced from `StageEvent`, `StageEvent.cost_usd` implemented for every LLM call site (NVIDIA rates only — HF asset-generation cost is not tracked), pre-generation cost/time estimate from real history, hard cap on paid LLM calls per reel
- **Phase 4b** ✅ — Publishing: OAuth connect-account flow (YouTube Data API + Instagram Graph API), `safe_to_publish` hard gate enforced at publish time, TikTok added as a third `CutPlatform` for render/review (publishing itself deliberately not implemented — see Key conventions), `scheduled`/`publishing`/`published` cut states wired end-to-end. Not done: attribution block in captions, TikTok publishing, unpublish/re-publish flows.
- **Phase 5** ✅ — Analytics: local-library music mixing with sidechain ducking (`LocalMusicSource` + `_build_ffmpeg_args`), HF asset-generation cost tracking (`engine/render/pricing.py`, `asset_hf_video`/`asset_hf_image` StageEvents), Wikipedia attribution block appended to published captions (`engine/publish/attribution.py`), post-publish metrics pull-back every 6h (`worker/tasks/metrics.py`), quality-vs-views surfaced on the reel list and per-cut engagement stats on the cut card.

## Worker queues

- **`generation` queue** — `generate_guide`, `enrich_context`, `publish_cut`, `pull_publish_metrics`. I/O-bound. Run with `--concurrency=4`.
- **`rendering` queue** — `render_cut`. CPU-bound. Run with `--concurrency=1`. Restarts after 10 tasks (`worker_max_tasks_per_child=10`) to prevent ffmpeg handle leaks.
- **Celery beat** — runs `reap_stuck_jobs` every 60 s and `pull_publish_metrics` every 6 h. Start with `celery -A worker.celery_app beat`.

## What is not yet wired in

- TikTok publishing — `TikTokPublisher.publish()` raises `NotImplementedError` on purpose (see Key conventions). Render/review works; only the upload call is missing.
- "Scheduled" has no trigger UI — the state machine and `publish_cut` both handle a cut already sitting in `scheduled`, but nothing currently transitions a cut *into* `scheduled` (no date/time picker, no Celery-beat-driven scheduled publish).
- Insight enrichment for the **standard LLM path** — currently only applied to structured-script beats
- Multi-image collage within a single beat — currently cycles sequentially; no side-by-side layout
- `PIXABAY_API_KEY` (`pixabay_api_key` in config) — Pixabay's public REST API has never documented a Music endpoint, so this stays unused by design; music sourcing uses `LocalMusicSource` instead (see Key conventions)
- Unpublish / re-publish flows — a published cut has no "take down" or "publish again" action
- Analytics beyond raw views/likes/comments — no trend charts, no engagement-rate normalization, no correlation report between `quality_score` and engagement (the reel-list Quality/Views columns are the raw inputs an operator would eyeball for that, not a computed correlation)

## Docs

- `docs/architecture.md` — system diagram, module breakdown, render pipeline detail
- `docs/data-model.md` — all tables, state machines, guide JSON schema
- `docs/api.md` — all HTTP endpoints, request/response shapes
- `docs/evaluation.md` — two-tier quality scoring, threshold/retry, tuning guide
- `docs/roadmap.md` — phase-by-phase plan with open items and sequencing rationale
