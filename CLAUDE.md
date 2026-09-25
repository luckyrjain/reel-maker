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
- **Per-reel TTS voice** — `Reel.tts_voice` (nullable, `None` = provider default) lets the create-reel form pick one of `engine/render/tts.py::CURATED_EDGE_VOICES` (a curated ~8-voice subset of edge-tts's ~400, not a free-text field — a typo'd voice would otherwise fail deep inside an async `edge_tts.Communicate()` call at render time, minutes into a Celery task, instead of at submission). `api/routers/reels.py::create_reel` validates the submitted value against the curated set and silently falls back to `None` for anything else (same "drop, don't 422" policy as unrecognized `platforms` values); `get_tts_provider(cache_dir, voice=...)` re-validates independently as defense in depth. **Only applies to `TTS_PROVIDER=edge`** — Kokoro's voice IDs (e.g. `af_heart`) are a different namespace than edge-tts's (e.g. `en-US-JennyNeural`), so an edge voice name is never forwarded to `KokoroProvider`; Kokoro always uses its own default, no per-reel choice for it yet.
- **Per-reel on-screen text color** — `Reel.text_color` (nullable, `None` = `compositor.py::DEFAULT_TEXT_COLOR`, `"white"`) lets the create-reel form pick one of `engine/render/compositor.py::CURATED_TEXT_COLORS`. This one is a **security-relevant** curated list, not just a UX one: `fontcolor={text_color}` is interpolated directly into the ffmpeg `drawtext` filter string with no escaping (unlike the text content itself, which `_escape_drawtext()` sanitizes) — an unvalidated value is a filter-graph injection point. Validated the same two-layer way as `tts_voice` (`create_reel` drops unrecognized values to `None`; `_build_text_filter()` re-validates independently), plus a dedicated test asserting an attempted `fontcolor=white:enable=0,...`-style payload is actually rejected, not just accidentally harmless.
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
.venv/bin/pytest                            # 676 tests across 30+ files, default run (4 test_compositor tests need ffmpeg
                                             # on PATH; 1 kokoro voice test skips without the kokoro package; 1 golden-reel
                                             # test is deselected by default — see below)
.venv/bin/pytest -m golden                  # the golden-reel smoke test (real edge-tts + real ffmpeg, ~20s, needs network)
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
- `task_acks_late=True` — broker acks only after task returns, so a killed worker's message is redelivered rather than silently lost. The redelivered message finds the job `running` (or `failed`) and no-ops, so recovery is the reaper failing the job and an operator retry, not an automatic re-run.
- `task_reject_on_worker_lost=True` — the message is redelivered when a worker is SIGKILLed, but the redelivery finds the job `running` (or already `failed` by the reaper) and no-ops; recovery is the reaper failing the job, not an automatic re-run.
- **Transient-failure retry** — `generate_guide` and `render_cut` retry up to twice with 30 s/60 s backoff when `worker/tasks/common.py::should_retry()` classifies the exception as transient (httpx transport errors, timeouts, HTTP 429/5xx). Deterministic failures — bad LLM JSON, quality-below-threshold, a missing row, ffmpeg's non-zero exit — fail once, unchanged. The retry branch resets `job.status` to `pending` before calling `self.retry()`: the idempotency guard rejects `running`, so a retry that left the status alone would be a silent no-op. It must **not** bump `job.attempts` — task entry already does. `enrich_context` and `publish_cut` stay `max_retries=0` on purpose (`publish_cut` is an irreversible external post: a transient error can arrive *after* the platform accepted the upload, and a retry would post twice). Database connection errors (`OperationalError`/`InterfaceError`) count as transient, and `api/db.py` sets `pool_pre_ping=True` (a backend killed server-side is replaced before use) and `hide_parameters=True` (SQLAlchemy errors otherwise echo bound values such as an OAuth token into logs and `job.error`). All of this lives in `worker/tasks/common.py::job_task` — never re-implement it in a task.
- **Idempotency guard / atomic claim** at task entry: only a `pending` job runs. `done`/`running` are redelivery no-ops and `failed` is terminal (an operator retry creates a new Job). The claim is `UPDATE … WHERE status='pending'`, so of two deliveries only one runs the body. The done-stamp is fenced the same way (`WHERE status='running'`): a worker the reaper already gave up on has its uncommitted mutations rolled back rather than committed.
- **Heartbeat** — `job_task` refreshes `job.heartbeat_at` every 30 s from a background thread while the body runs (bodies block for minutes on an LLM call, ffmpeg or an upload); bodies also call `heartbeat()` for progress. `reap_stuck_jobs` (Celery beat, every 60 s, routed to the `generation` queue — an unrouted beat task lands on the default queue that no documented worker consumes) fails any `running` job without a heartbeat update in the last 5 minutes, **and** any `pending` job whose `updated_at` is older than `PENDING_STALE_MINUTES` (240; a message lost after a successful enqueue, or no worker consuming the queue). Routers fail a job whose `.delay()` raised at once (`fail_unenqueued`), so the long threshold only covers messages lost after a successful enqueue. The pending branch keys on `updated_at`, not `created_at`, so a job sitting in retry backoff is not reaped for being old. Each reap is a compare-and-set that re-checks the status and staleness the SELECT saw, and rolls back only the owner state that job's type owns (`JOB_IN_FLIGHT`). The `pending` threshold is deliberately long because a job legitimately queues (render runs at concurrency 1 and a render can take an hour; four generation slots can each be busy for hours) and a reaped job is terminal. NULL timestamps are handled with `COALESCE` (rows from before `heartbeat_at` existed). Each task declares `max_runtime_s` (enrich 30 min, generate 4 h — worst case from the LLM timeouts is ~3.2 h — render 60 min, publish 60 min) and passes it to Celery as `soft_time_limit` (`time_limits()`; hard limit +120 s): `SoftTimeLimitExceeded` fails the job with an accurate error and, if ignored, Celery escalates to SIGTERM then SIGKILL, which is the only thing that frees a hung worker slot. This is best-effort, not a guarantee — a plain SIGKILL, or `_fail_interrupted` losing a race against a DB write on a worker under load, skips the fast path entirely — so the reaper remains the real backstop, not a fallback that rarely matters. Past the cap the heartbeat thread also stops beating so the reaper fails the record. The reaper beat message expires after 55 s so a starved `generation` queue does not drain a backlog in a burst.
- **Atomic MP4 write** — FFmpeg writes to `.tmp.mp4`, then `os.replace()` to the final path; a killed process never leaves a servable half-written file.

## Module layout

```
api/
  main.py             FastAPI app factory; static files, Jinja2, all routers; lifespan hook
                      runs validate_configured_models() at startup (best-effort, never blocks —
                      see engine/generation/llm.py)
  config.py           pydantic-settings Settings; includes nvidia_api_key, nvidia_generation_model, use_nvidia_for_generation, pexels_api_key, huggingface_api_key, huggingface_image_model, huggingface_video_model, pixabay_api_key, credentials_key, max_paid_llm_calls_per_reel, nvidia_price_per_1m_input_tokens, nvidia_price_per_1m_output_tokens, public_base_url, youtube_oauth_client_id/secret, meta_oauth_app_id/secret, music_library_dir, huggingface_price_per_image, huggingface_price_per_video_second, evaluator_axis_weight_multipliers
                      (dict[str, float], default `{}` — the first dict-typed Settings field; pydantic-settings
                      parses its env var as JSON, e.g. `EVALUATOR_AXIS_WEIGHT_MULTIPLIERS='{"insight": 0.5}'`)
  crypto.py           Fernet seal/open_ + Encrypted SQLAlchemy TypeDecorator
  db.py               SQLAlchemy engine, SessionLocal, get_db() dependency
  models.py           All ORM models + enums; includes StageEvent, PerformanceNote
  oauth.py            Generic OAuth2 authorization-code flow — YouTubeOAuth, InstagramOAuth,
                      new_state()/consume_state() (process-local CSRF state, single-operator tool)
  schemas.py          Pydantic schemas for JSON endpoints
  state.py            REEL_TRANSITIONS, CUT_TRANSITIONS, transition(); JOB_IN_FLIGHT — the owner state each
                      job type rolls back to `failed` (explicit: guide_ready/in_review are deliberately absent)
  routers/
    reels.py          POST /api/reels (accepts generation_path + platforms), GET /api/reels (list),
                      GET /api/reels/{id} (pipeline cost/latency/quality panel), POST /api/reels/estimate
    jobs.py           GET /api/jobs/{id} (JSON + HTML fragment)
    cuts.py           POST /render, PATCH, POST /approve, POST /publish, render-status,
                      publish-status, video stream, thumbnail stream + POST /thumbnail
                      (choose a candidate), POST /hook-variant (swap the hook beat's vo_script),
                      GET /subtitles (SRT caption file, same path-traversal guard as the video/
                      thumbnail streams — Phase 5d)
    credentials.py    GET /api/credentials (connect/disconnect UI), GET /{provider}/authorize,
                      GET /{provider}/callback, POST /{provider}/disconnect
    insights.py       GET /api/insights (quality↔engagement correlation + top/bottom performer
                      report + performance-notes UI, one page — see engine/analytics/correlation.py);
                      POST /api/insights/notes (create), POST /api/insights/notes/{id}/toggle,
                      DELETE /api/insights/notes/{id} — all three return the
                      fragments/performance_notes.html partial for htmx swap

worker/
  celery_app.py       Celery instance; acks_late=True, beat schedule, split queues
  tasks/
    common.py         job_task() — the shared Job lifecycle decorator (atomic claim, heartbeat thread, atomic
                      fenced done-stamp, transient-retry reset, failure stamp + per-job-type owner rollback via
                      api/state.py::JOB_IN_FLIGHT); rollback_owner(); should_retry() / is_transient_error() /
                      heartbeat() — shared task helpers
    generate.py       generate_guide(job_id) — prepare (reel must be `generating`, paid-call budget), enrichment,
                      conflict injection, visuals LLM, closed-loop eval retry, observability,
                      paid-call budget cap (_enforce_paid_call_budget); after an accepted guide, one
                      best-effort extra call via generate_hook_variants() stores alt hook lines on
                      every cut of the reel (same list — beats are normally identical across platforms);
                      queries active PerformanceNote rows unconditionally at function entry (both
                      generation paths reach the shared job.meta write that records
                      performance_note_ids) and seeds them into the standard path's prior_feedback
                      from attempt 1 — see Key conventions below; passes
                      settings.evaluator_axis_weight_multipliers to every score_guide() call (both paths)
    render.py         render_cut(job_id) — prepare (refuses an already-posted cut), resolve_or_reuse,
                      synth_to_budget, TTS-accurate timecodes, atomic MP4, observability;
                      resolves a music track via get_music_sourcer().find(music_cue) and
                      passes it to composite_cut(); stores all returned thumbnail candidates on
                      cut.thumbnail_candidates, cut.thumbnail_path defaults to candidates[0];
                      tracks which beats' resolve_or_reuse() call returned no real media at all
                      ([(None, None)], resolve_beat_assets()'s "nothing found anywhere in the
                      chain" sentinel) and writes the list to cut.black_frame_beat_indices
                      (Phase 7a) — a re-render replaces it wholesale, same as thumbnail_candidates;
                      unpacks composite_cut()'s 3rd return value onto cut.subtitle_path (Phase 5d),
                      None when the render produced nothing to caption — same wholesale-replace
                      policy; sets cut.rendered_pins_fingerprint = compute_pins_fingerprint(db, cut.id)
                      at the exact point video_path/thumbnail_path/duration_s are already assigned —
                      a snapshot of the pins that built THIS video, for the publish-time staleness
                      gate below (see Key conventions)
    publish.py        publish_cut(job_id) — safe_to_publish gate, video-matches-pins staleness
                      gate (assert_video_matches_pins(), same branch/timing as assert_safe_to_publish,
                      see Key conventions), builds caption via
                      build_published_caption() (appends attribution block), dispatches to
                      engine/publish/registry.py, commits platform_post_id the moment the post is
                      live (a re-run with an id set finalizes without uploading again); max_retries=0
    maintenance.py    reap_stuck_jobs() — Celery beat task; fails stale running + pending jobs via
                      compare-and-set, rolling back only the owner state that job type owns
    metrics.py        pull_publish_metrics() — Celery beat task (every 6h); pulls views/likes/
                      comments for published cuts via get_metrics_fetcher(), skips platforms/
                      credentials without a fetcher, commits per-cut, tolerates per-cut failures

engine/
  observability.py    record_stage() context manager — writes StageEvent rows on exit;
                      paid_call_count() — counts nvidia-provider StageEvents for a reel (budget cap);
                      latest_quality_scores(jobs) — latest non-null quality_score per reel_id from a
                      list of Job rows (last write wins, ascending created_at); the single shared
                      implementation behind api/routers/reels.py's list/detail pages AND
                      engine/analytics/correlation.py — extracted so a third near-identical copy
                      wasn't added for the correlation feature (see CLAUDE.md's own heartbeat() note
                      on why this codebase avoids copy-drift)
  analytics/
    correlation.py    quality_engagement_correlation(db) -> CorrelationResult (Pearson r + sample_size +
                      insufficient_variance flag; refuses to compute below MIN_SAMPLE=5 reels or when
                      either series has zero variance — np.corrcoef returns NaN on zero variance, guarded
                      explicitly rather than let "nan" leak into the UI string); top_bottom_performers(db, k=3)
                      -> (top, bottom) sorted by views desc/asc, or one combined list when n < 2k. Both read
                      `Cut.views` (max per reel, matching _reel_list_metrics()'s existing "views" definition)
                      against latest_quality_scores() — no new schema, no cached/denormalized column, no
                      chart library (see docs/specs/2026-09-phase5-quality-engagement-feedback.md §2.1). New
                      package (not observability.py) — this is the first cross-reel "insights" feature,
                      not per-reel instrumentation
  generation/
    guide_schema.py   Beat, PlatformGuide, MasterGuide Pydantic models — platform Literal includes tiktok
    llm.py            LLMProvider + OllamaProvider (captures last_usage/total_usage token counts);
                      get_llm_provider(), get_enrichment_provider(), is_nvidia_generation();
                      check_model_available()/validate_configured_models() — best-effort startup
                      ping of {base_url}/models for both configured models, deduped by
                      (base_url, model); called from api/main.py's lifespan hook, never raises,
                      never blocks startup (Phase 7c)
    pricing.py        llm_cost_usd() — NVIDIA per-token cost from Settings rates (0 until configured)
    estimate.py       estimate_generation() — pre-generation call-count/time/cost estimate for the
                      create-reel form, cost sourced from this operator's own StageEvent history
    prompt.py         build_messages(prior_feedback=) + build_visuals_messages() — visuals system prompt anchors LLM to per-beat VO only
    script_parser.py  BeatStub + parse() + is_structured() — structured-script extractor
    visual_fallback.py  Fallback visual_direction synthesis — section/VO keyword tables
    beat_enrichment.py  Tactical insight enrichment + conflict-beat synthesis (topic-fenced)
    evaluator.py      score_guide(context, guide, target_length_s, axis_multipliers=None) — 17-axis rule
                      scorer (0–100); axis_multipliers (from Settings.evaluator_axis_weight_multipliers)
                      scales a named axis's deduction via one small correction block immediately before
                      the final return, reusing the deductions dict already built for the issue-string
                      breakdown — None/empty is a byte-identical no-op to every prior test in this file;
                      Script→Visual Alignment (20 pts) redistributes across only the sub-signals
                      (entity/action/context) that actually apply to the content, full credit when
                      none do — a non-football niche whose VO never triggers a person name/action
                      verb/event-year regex must not have this whole axis collapse to a flat deduction
                      (Phase 6c fairness fix, see docs/roadmap.md); Visual Variety (5 pts) skips its
                      deduction when every beat's visual_direction is uncategorized "other" — its 6
                      categories are unconditionally football vocabulary with no niche gate at all,
                      unlike the axes below that swap in a _UNIVERSAL regex; see docs/evaluation.md
    llm_judge.py      judge_guide() — LLM semantic judge; 5 dims × 0–20 = 100 pts
    postprocess.py    clean_guide() — strips label prefixes; derives up to 5 on_screen_text segments
    hook_variants.py  generate_hook_variants() — one best-effort LLM call for N_VARIANTS (3) alternate
                      hook-beat lines, given the current hook vo_script + context + niche; [] on any
                      failure (never raises — this runs after the guide already cleared the quality gate)
  render/
    asset_sourcer.py  PexelsVideoSource + WikipediaImageSource (with license metadata fetch);
                      HuggingFaceImageSource (FLUX.1-schnell) + HuggingFaceVideoSource (LTX-Video);
                      fallback chain: Wikipedia → Pexels → HF Video → HF Image → None;
                      resolve_or_reuse() — pins assets by beat_index + fingerprint, reuses on re-render;
                      resolve_beat_assets() still available for non-pinned use; records "asset_hf_video"/
                      "asset_hf_image" StageEvents (cost_usd only on an actual generation call, not a
                      cache hit — tracked via last_call_was_generated); LocalMusicSource.find(music_cue) —
                      keyword-overlap match against Settings.music_library_dir, no external API;
                      compute_pins_fingerprint(db, cut_id) -> str | None — colocated with _fp() and
                      resolve_or_reuse() as their natural companion; deterministic sha256 fingerprint
                      of every CutAsset currently bound to a cut (sorted (beat_index, order_in_beat,
                      asset_id) tuples, so query result ordering never changes the hash); None when
                      the cut has zero bound pins. Shared by render.py (writer, at render success)
                      and gate.py (reader, at publish time) so there is exactly one implementation —
                      see Key conventions
    pricing.py        hf_image_cost_usd() / hf_video_cost_usd(duration_s) — config-driven HF
                      asset-generation cost, mirrors engine/generation/pricing.py's honesty policy
                      (0.0 until the operator sets a real per-unit price)
    tts.py            EdgeTTSProvider.synthesize() + .synth_to_budget(); _audio_duration() helper
    captions.py       transcribe_audio() -> TranscriptResult (Phase 5d, was a bare
                      list[CaptionSegment]) — one Whisper model.transcribe() call now serves both
                      .words (existing shape/values, byte-identical, still beat-relative) and
                      .segments (new — one CaptionSegment per Whisper segment, full sentence text)
                      so a render doesn't pay for transcription twice; no-op (empty
                      TranscriptResult) if openai-whisper isn't installed. beat_offset_s keeps its
                      pre-existing beat-relative meaning for BOTH fields — see this file's Key
                      conventions entry on offset handling, this is the single most safety-critical
                      rule in this module
    srt.py            write_srt(cues, path) -> Path | None (Phase 5d, new module) — pure
                      list[CaptionSegment] -> .srt formatter (sequential numbering, HH:MM:SS,mmm
                      timestamps), no ffmpeg/network; None (writes nothing) for empty cues, same
                      "degrade gracefully" posture as the rest of this package. Input is already
                      shifted to the reel's absolute timeline by the caller (compositor.py) — this
                      module does no offset math of its own
    compositor.py     composite_cut() — MoviePy stage + FFmpeg drawtext stage; returns
                      (duration, thumbnail_candidates, subtitle_path) — a 3-tuple as of Phase 5d,
                      was a 2-tuple. _write_thumbnail_candidates() samples 4 frames
                      across the reel (the original ~0.5s-in frame first, at thumbnail_path itself, plus
                      3 more at thumbnail_path's stem + `_1`/`_2`/`_3`) so a caller that only reads
                      candidates[0] sees the exact pre-existing single-frame behavior;
                      120ms audio fade in/out per beat (moviepy.audio.fx.AudioFadeIn/AudioFadeOut via .with_effects()) for smooth narration transitions;
                      _build_text_filter() uses Whisper timestamps when available, proportional fallback;
                      takes text_color (default DEFAULT_TEXT_COLOR="white") — validated against
                      CURATED_TEXT_COLORS before being interpolated unescaped into the drawtext filter
                      string, since unlike the text content it is NOT run through _escape_drawtext();
                      accepts music_path — _build_ffmpeg_args() adds a sidechain-ducked (VO present) or
                      plain-volume (no VO) music mix, always atrim'd to the render's total_duration;
                      atomic final write via os.replace(); _build_beat_transcripts() (Phase 5d) is the
                      one call site for transcribe_audio(), always at its default beat_offset_s=0.0 —
                      see this file's Key conventions entry, this is a mutation-tested regression
                      guard, not just a docstring claim; after the MP4 write, builds SRT cues from
                      each beat's TranscriptResult.segments (falling back to
                      _proportional_caption_cues() — the same vo_script-sentence-split regex
                      _build_text_filter()'s own no-Whisper fallback already uses — when a beat's
                      .segments came back empty), shifts them to the reel's absolute timeline via an
                      explicit running sum over beat_durations (NOT the first loop's `t`, which has
                      already run to completion by that point), and calls srt.write_srt()
  publish/
    base.py           Publisher interface + PublishResult dataclass; publish() takes an explicit
                      caption: str param (not cut.caption) so attribution text can be injected
                      without polluting the DB-stored caption
    registry.py       get_publisher(platform) / credential_provider_for_platform(platform) /
                      get_metrics_fetcher(platform) — the last returns None (not a raise) for an
                      unmapped platform (e.g. tiktok)
    gate.py           assert_safe_to_publish() / unsafe_assets() — enforces Asset.safe_to_publish
                      before any publish call; the one place that field is actually checked;
                      assert_video_matches_pins(db, cut) (Phase 7e) — compares cut.
                      rendered_pins_fingerprint against a freshly computed
                      compute_pins_fingerprint(db, cut.id); raises ValueError on mismatch, no-ops
                      when rendered_pins_fingerprint is None (legacy/rollout-safety skip — see Key
                      conventions); takes a Cut object, deliberately asymmetric with
                      assert_safe_to_publish's cut_id — publish_cut is the sole caller of both and
                      already has the Cut loaded
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
                      token via the stored refresh_token first; after a successful upload, if
                      cut.subtitle_path is set, one best-effort POST to captions.insert
                      (_upload_captions() — multipart/related JSON snippet + .srt media part, built
                      by _build_multipart_related() rather than httpx's files= param, which sends
                      multipart/form-data — a different wire format captions.insert doesn't expect)
                      (Phase 5d) — wrapped in record_stage(..., "captions_upload", ...) with the
                      try/except placed INSIDE the with block so a failure never fails publish_cut
                      but still lands as a real ok=False StageEvent; see this file's Key conventions
                      entry on this record_stage composition rule, mutation-tested both wrong ways
                      during implementation. YouTube's Captions API accepting raw SRT bytes as
                      documented is a design-flagged assumption, not yet live-verified against a
                      real account. YouTubeOAuth.scope (api/oauth.py) includes youtube.force-ssl
                      alongside youtube.upload specifically for captions.insert — youtube.upload
                      alone is documented as insufficient for it (an already-connected account
                      needs to reconnect to pick up the wider grant; independent review caught this
                      before merge — see docs/roadmap.md's 5d section)
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
    0006_variants.py  Cut.thumbnail_candidates + Cut.hook_variants columns
    0007_performance_notes.py  performance_notes table (id, text, active, created_at)
    0008_tts_voice.py   Reel.tts_voice column
    0009_text_color.py  Reel.text_color column
    0010_black_frame_visibility.py  Cut.black_frame_beat_indices column
    0011_subtitle_caption_export.py  Cut.subtitle_path column
    0012_rendered_pins_fingerprint.py  Cut.rendered_pins_fingerprint column

tests/
  test_evaluator.py           41 tests — all 17 evaluator axes + helpers, multi-platform dedupe,
                              axis_multipliers no-op default (None/{} byte-identical to every existing
                              fixture), zero/doubled/unknown-axis multiplier correction; non-football
                              niche fairness (Phase 6c) — realistic finance/fitness fixtures no longer
                              unfairly capped, alignment axis gives full credit when no sub-signal
                              applies (regression-guarded against also disabling real mismatches),
                              visual variety not penalized when every beat is uncategorized "other"
                              (regression-guarded against also disabling real repetition within a
                              recognized football category, and against the known "mostly other"
                              residual gap — see docs/roadmap.md Phase 6c); assertions read the
                              deduction directly off the "Score breakdown" issue line
                              (`_axis_deduction()`) rather than an axis-specific issue message, since
                              at least one such message (alignment's) is gated behind a narrower
                              condition that stays empty regardless of the deduction's real value —
                              an independent review caught the original assertions passing vacuously
                              against a full revert of the fix
  test_script_parser.py       11 tests — parse() routing, beat splitting, _derive_on_screen
  test_state.py               11 tests — REEL_TRANSITIONS, CUT_TRANSITIONS, invalid moves
  test_enrichment.py          15 tests — coerce_beat_type, _enrich_batch response parsing, topic fence
  test_audio_text_sync.py     16 tests — clean_guide() regeneration, _build_text_filter() proportional timing + whisper fallback, PATCH re-derivation, visual direction anchoring;
                              TranscriptResult (Phase 5d) — .words/.segments derived from one transcribe() call, beat_offset_s shifts
                              both fields identically, empty-without-whisper degrade; offset-handling regression guard —
                              _build_beat_transcripts() never passes a non-zero per-beat offset into transcribe_audio() (mutation-
                              tested against the exact double-shift bug the design review caught) and a companion numeric-value
                              test confirming _whisper_timestamps() output for a multi-beat reel is not doubled
  test_srt.py                  8 tests — write_srt() (Phase 5d): timestamp formatting incl. hour-boundary and
                              millisecond-rounding, sequential numbering, empty-cues returns None with no file written,
                              multi-cue/multi-beat concatenation with pre-shifted absolute offsets, parent-dir creation
  test_context_enricher.py    13 tests — evaluate_context axes, llm_enrich
  test_enrich_context_task.py 14 tests — enrichment gating, LLM failure fallback, structured script guard, missing reel, owner rollback wiring, orphan generate-job cleanup (only when it claimed the orphan)
  test_maintenance.py         40 tests — reaper on in-memory SQLite: per-job-type owner rollback, stale running/pending jobs, per-job-type pending thresholds, healthy jobs untouched, updated_at keying, compare-and-set back-off, status pin = SELECT snapshot, one bad job doesn't stop the rest, done-orphan sweep (lost after_commit_failed fail-stamp) per job type, existing-error/freshness exclusions
  test_tts.py                  16 tests — provider selection, unknown-provider fallback, SilentProvider shared file, synth_to_budget
                              clamp; synthesize() network hardening (Phase 7d follow-up) — atomic tmp-then-replace write (the
                              `tmp.replace(out)` call itself is inside the same try/except as the download, so a failed rename
                              cleans up the `.tmp` file too, not just a failed download) so a failed or timed-out synth never
                              leaves a truncated file at the cache path (the `if out.exists()` cache check would otherwise reuse
                              it forever), one retry after a 60s timeout on the edge-tts network call rather than an indefinite
                              stall; each failure mode (timeout, download error, rename error) is individually mutation-verified
                              against a version missing that specific guard
  test_common.py              18 tests — transient-error classification (incl. DB connection errors), retry budget
  test_job_lifecycle.py      122 tests — job_task on dummy tasks (in-memory SQLite): atomic claim/race, fenced done-stamp and heartbeat (JobLost), heartbeat thread (survives DB blips, never touches a reaped job, stops at max_runtime_s, never outlives the task), prepare-before-attempts, retry/backoff, refused retry (Reject, incl. an unclaimed job), failure stamp (NUL/surrogates/[parameters]/DETAIL/CONTEXT redacted incl. multi-line, reaped job, masked errors, discarded half-writes), DB errors, fail-fast on shutdown/hard-kill, dead-connection recovery in the terminal failure recorders (incl. InterfaceError, fresh-session close on both a successful and a failed retry attempt), per-job-type owner rollback (fresh state), after_commit + cleanup hook (incl. a shutdown during the hook itself, a hook that itself raises without discarding the failure stamp, and a multi-write hook rolling back atomically via its own SAVEPOINT), `.delay` signature regression, per-task job_type/max_retries/runtime wiring, beat-task routing, pool_pre_ping
  test_r3_proposed.py         38 tests — regressions found by round-3 mutation testing: distinct job/reel/cut ids so an id mix-up can't hide, transaction-visibility checks via a second connection, _error_text regex boundaries, refused-retry/reaped-job edge cases, template hx-post assertions, per-type pending thresholds
  test_r4_gaps.py             30 tests — regressions found by round-4 mutation testing: heartbeat commit visibility, failure-path rollback of flushed rows, commit-failure-at-done-stamp is not "done", after_commit cleanup without a hook, pool_pre_ping, task signature
  test_tasks_real_db.py       7 tests — real tasks through job_task on SQLite: post id durable after a post-upload failure, built caption sent, enrich enqueues the real job id;
                              YouTube captions-upload best-effort behavior (Phase 5d, real DB — this is the row a mocked
                              db can't give you) — a forced captions.insert failure does not fail publish_cut or affect
                              platform_post_id/published_at AND produces a real StageEvent(stage="captions_upload",
                              ok=False, detail.error=...) row (mutation-tested against both wrong-composition bugs:
                              no inner try/except propagating the failure, and catching without setting ev.ok); success
                              path records ok=True; no subtitle_path means no captions_upload attempt or StageEvent at all
  test_generate_task.py       11 tests — missing reel, reel not generating, paid-call budget, structured-path fallback (incl. a soft-limit kill), music_cue default, caption/hashtags does not swallow a runtime-limit timeout;
                              2 regression tests (mutation-tested against the naive/buggy version first):
                              seeded PerformanceNotes survive past attempt 1 on retry (the retry-replace bug),
                              structured-path success doesn't NameError on performance_note_ids (the wrong-branch-query bug)
  test_render_task.py          14 tests — missing cut, already-posted cut refused, success clears stale error, music wiring, reel.tts_voice/text_color threaded into get_tts_provider()/composite_cut(), black_frame_beat_indices flagged when a beat resolves no real media and stays None when every beat does;
                              subtitle_path (Phase 5d) — populated from composite_cut()'s 3rd return value on a successful
                              render, reset to None (not left stale) on a re-render that produces zero cues;
                              rendered_pins_fingerprint (Phase 7e) — call-and-assign wiring only (this file is
                              100%-MagicMock-based, resolve_or_reuse itself is patched, no real CutAsset row ever
                              exists here): compute_pins_fingerprint is patched and asserted called with cut.id,
                              its return value asserted on cut.rendered_pins_fingerprint — the stronger "reflects
                              real pins" property is test_publish_gate.py's staleness end-to-end test, not this file's
  test_hook_variants.py        9 tests — generate_hook_variants(): JSON-list/dict-wrapped parsing, excludes a variant identical to the original, caps at N_VARIANTS, empty hook skips the LLM call, malformed JSON/non-list/non-string items/provider exception all degrade to []
  test_asset_sourcer.py        12 tests — resolve_or_reuse pin, reuse, re-pin, beat isolation, commits (no open transaction on either resolve or reuse path), Wikipedia names all searched before any asset is flushed;
                              compute_pins_fingerprint (Phase 7e) — deterministic for a given pin set,
                              order-independent w.r.t. query result ordering, None for zero pins, changes when a
                              beat's pin changes, unaffected when an untouched beat's pin stays the same
  test_asset_sourcer_cost.py   9 tests — HF cost StageEvents charged only on real generation, not cache hits
  test_music_source.py         8 tests — LocalMusicSource keyword matching, missing/empty library
  test_llm_judge.py            3 tests — neutral-score fallback on raise, garbage, out-of-range
  test_reels_router.py        25 tests — reel list/detail routes, platform-selection form, pipeline panel, htmx id/target consistency, quality/views columns, published-cut engagement stats, enqueue failure fails fast (503) instead of polling forever, no transaction open across .delay(); tts_voice/text_color selection — explicit choice stored, unknown value dropped to None, omitted defaults to None
  test_pricing.py              4 tests — llm_cost_usd() rate application, zero-rate default
  test_llm_provider.py         12 tests — OllamaProvider last_usage/total_usage capture;
                              check_model_available()/validate_configured_models() — model listed/
                              not listed/timeout/network error/non-2xx, dedup by (base_url, model),
                              local-Ollama vs NVIDIA-NIM warning wording
  test_main.py                 2 tests — lifespan hook logs a warning per misconfigured model,
                              never crashes startup when every model checks out
  test_estimate.py             8 tests — generation path resolution, historical cost averaging incl. structured-fallback exclusion
  test_observability.py        3 tests — paid_call_count() scoping and filtering
  test_correlation.py         12 tests — quality_engagement_correlation() (below-MIN_SAMPLE, zero-variance
                              quality/views, a hand-computed synthetic dataset checked to a fixed tolerance
                              — not trusted circularly via numpy, multi-cut-per-reel uses the max-views cut,
                              quality-without-views/views-without-quality excluded); top_bottom_performers()
                              (n < 2k combined list, n >= 2k top/bottom no-overlap, ties, a max-views cut
                              with no guide degrades to hook_vo=None instead of crashing)
  test_insights_router.py      9 tests — GET /api/insights with 0/some/enough data; PerformanceNote
                              create/toggle/delete round-trip through the DB; deleted note gone from a
                              fresh GET; unknown-id toggle/delete (404 / no-op)
  test_config.py               2 tests — Settings.evaluator_axis_weight_multipliers defaults to `{}`;
                              its env var (JSON) round-trips to a real dict of floats, not a string —
                              the first dict-typed Settings field in this codebase
  test_publish_gate.py         7 tests — safe_to_publish enforcement; assert_video_matches_pins (Phase 7e) —
                              matching fingerprint doesn't raise, mismatch raises an actionable message,
                              rendered_pins_fingerprint is None does NOT block even with real current pins
                              (mutation-tested — the rollout-safety property design §7 depends on); one
                              end-to-end regression test reproducing the exact bug sequence with real
                              resolve_or_reuse() calls (mutation-verified against a reverted fix)
  test_oauth.py               13 tests — OAuth state CSRF, YouTube/Instagram authorize+exchange, long-lived token swap, token-in-header regression
  test_credentials_router.py   9 tests — connect/callback/disconnect routes
  test_publish_task.py        12 tests — publish_cut safety gate (publisher never reached, also on the finalize path), no auto-retry, post id committed early, re-run finalizes without re-upload, attribution caption;
                              assert_video_matches_pins wiring (Phase 7e) — a mismatch blocks publish before
                              any credential lookup; a stronger finalize-branch-exemption test using a REAL
                              mismatched fingerprint (not the fixture's default None, which can't by itself
                              distinguish correct wiring from a misplaced call) — mutation-tested by moving
                              the call into the finalize branch and confirming this specific test fails;
                              _cut()'s default explicitly sets rendered_pins_fingerprint = None (a
                              MagicMock's unconfigured attribute is truthy and non-None, which would
                              otherwise trip the new check on every pre-existing uploading-branch test)
  test_publish_registry.py     7 tests — platform→publisher, platform→credential-provider, platform→metrics-fetcher mapping
  test_cuts_publish_router.py 22 tests — POST /cuts/{id}/publish state-guard and enqueue; render refused for an already-posted cut; enqueue failure fails fast (503) and frees the cut; row lock (incl. update_cut reads its body before locking); failed-cut card
  test_youtube_publisher.py    9 tests — resumable upload flow, token refresh, whitespace-caption fallback (best-effort
                              captions-upload StageEvent coverage lives in test_tasks_real_db.py, which needs a real DB
                              row to assert on — see below); _upload_captions() request-construction regression tests
                              (Phase 5d review fixes) — token stays in the Authorization header, never in params/URL,
                              and the body is real multipart/related (no Content-Disposition), not multipart/form-data
  test_instagram_publisher.py  7 tests — container create/poll/publish flow, error paths, token-in-header regression
  test_attribution.py          8 tests — build_attribution_block dedup/formatting, build_published_caption
  test_metrics_fetcher.py      6 tests — YouTube/Instagram metrics parsing, token-in-header regression
  test_metrics_task.py         6 tests — pull_publish_metrics fetcher/credential skip paths, per-cut failure isolation
  test_compositor.py           14 tests — _build_ffmpeg_args no-music/sidechain/no-VO branches, real ffmpeg music-mixing end-to-end (4 of these need ffmpeg on PATH), _write_thumbnail_candidates (no ffmpeg — a fake clip object) covering first-candidate-is-original-path, distinct sibling files, short-clip clamping; _build_text_filter text_color — default, a curated color, an uncurated value falling back to default, a filter-graph-injection attempt rejected;
                              the 4 real-ffmpeg composite_cut() tests also assert a real .srt file is produced with real
                              Whisper-or-fallback timing (Phase 5d) — the no-VO/no-vo_script case asserts subtitle_path
                              is None instead (nothing anywhere in the fallback chain to caption)
  test_golden_reel.py          1 test (marked `golden`, deselected by default — see Commands) — real edge-tts synthesis
                              + real ffmpeg composite_cut(), no mocks anywhere in the chain; asserts real video+audio
                              streams at the correct dimensions; mutation-verified against the real historical
                              zero-audio bug (reintroduced .audio_fadein()/.audio_fadeout(), confirmed this test fails
                              with the same error the live incident produced, restored the fix)
  test_variants_router.py      15 tests — POST /cuts/{id}/hook-variant (swap + rederive on_screen_text, wrong status, out-of-range index, no variants), POST /cuts/{id}/thumbnail (choose, wrong status, out-of-range, no candidates), GET /cuts/{id}/thumbnail/{index} (serves file, 404 out-of-range, 403 outside VIDEO_STORE_DIR);
                              GET /cuts/{id}/subtitles (Phase 5d) — serves the .srt file with the correct Content-Type,
                              404 when subtitle_path is unset or the cut doesn't exist, 403 outside VIDEO_STORE_DIR
                              (same path-traversal guard as stream_video/stream_thumbnail)

ui/templates/
  index.html          Context-entry form; niche/platform picker + target_length + voiceover_mode +
                      generation_path selects; live cost/time estimate (htmx → cost_estimate.html)
  reels_list.html     Paginated reel list (GET /api/reels) — status badges, per-cut platform badges,
                      Quality (latest job's quality_score) and Views (max across the reel's cuts) columns
  reel.html           Page shell — pipeline cost/latency/quality panel, loops cuts, includes cut_card.html
  credentials.html    Connected-accounts page — connect/disconnect per provider, TikTok shown as
                      not-yet-available
  insights.html       Quality↔engagement correlation (r + sample_size, always with the statistical-honesty
                      caveat text — never r alone) + top/bottom (or combined, n<6) performer table
                      (hook line from the max-views cut) + performance-notes form/list — GET /api/insights.
                      Linked from index.html and reels_list.html as a plain `<a>` (no shared nav template
                      in this app — see api/routers/insights.py)
  fragments/
    cut_card.html     Full cut card; read-only or editable (in_review); render/approve/publish actions
                      per CutStatus branch; published branch shows views/likes/comments once
                      metrics_updated_at is set, else a "checked every 6h" hint; "Download captions
                      (.srt)" link next to the MP4 download, visible whenever cut.subtitle_path is
                      set (Phase 5d) — not gated to in_review, same visibility as the video download
    render_status.html   Polling fragment; video + Approve/Re-render when done
    publish_status.html  Polling fragment (id="publish-status-{cut.id}", distinct from its parent
                      "publish-section-{cut.id}" target — do not reuse the parent's id, that
                      creates nested duplicate DOM ids on swap)
    cost_estimate.html   Pre-generation estimate fragment (POST /api/reels/estimate)
    performance_notes.html  htmx-swappable PerformanceNote list (checkbox = active toggle, × = delete);
                      returned by all three POST/DELETE /api/insights/notes* routes
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

- `reels` — master concept; `status` tracks generation phase; `tts_voice` (nullable) is a per-reel edge-tts voice override, `None` = provider default; `text_color` (nullable) is a per-reel on-screen text color override, `None` = `"white"` (see Key conventions for both — `text_color` in particular is validated as a filter-injection guard, not just a UX curation)
- `cuts` — one row per platform (`youtube_shorts`, `instagram_reels`, or `tiktok`); holds `guide` (JSONB), `caption`, `hashtags`, `video_path`, `thumbnail_path`, `thumbnail_candidates` (JSON list of every frame `render_cut` sampled — `thumbnail_path` is whichever one is currently chosen, `[0]` by default), `hook_variants` (JSON list of alternate hook-beat lines generated once per guide, `None` if generation failed or the paid-call budget was already spent), `black_frame_beat_indices` (JSON list of 0-indexed beats that got no real media anywhere in the asset-sourcer fallback chain and rendered as a black frame; `None` when every beat resolved something — Phase 7a operator visibility, see Key conventions), `rendered_pins_fingerprint` (sha256 fingerprint of the `CutAsset` pins that built the currently-stored `video_path`, snapshotted by `render_cut` at render success; `None` means "not yet rendered" or "rendered before this column existed" — Phase 7e publish-time staleness gate, see Key conventions), `subtitle_path` (path to the SRT caption file `composite_cut()` writes alongside the MP4; `None` when the render produced nothing to caption, e.g. silent `voiceover_mode` — Phase 5d, replaced wholesale on re-render like the other render-artifact columns above), `platform_post_id`, `published_at`, `views`, `likes`, `comments`, `metrics_updated_at` (last three populated by `pull_publish_metrics()`, `None` until the first successful pull)
- `assets` — cached media files; deduplicated by `(source, source_ref)`; `source` is `pexels`, `wikipedia`, `huggingface`, or `huggingface_video`; `type` is `footage` or `photo`; includes `license_url`, `attribution`, `safe_to_publish`. HF-generated assets are `safe_to_publish=True`.
- `cut_assets` — per-beat asset binding ledger; `beat_index` + `order_in_beat` identify position; `resolved_from` is `sha256(visual_direction)[:16]` for change detection; unique constraint on `(cut_id, beat_index, order_in_beat)`; `start_s`/`end_s` updated after TTS measurement
- `jobs` — every async operation (`enrich`, `generate`, `render`, `publish`); includes `started_at`, `heartbeat_at`, `meta` (JSON — stores `generation_path`, `path`, `stub_count`, `quality_score` on success, and optionally `structured_score`/`structured_fallback` when structured path fell back to standard, and `performance_note_ids` — the active `PerformanceNote` ids at the time of this generate run, written by the same shared `job.meta` line as `quality_score`); `error` is `None` on success, set to exception message on failure only
- `stage_events` — instrumentation: one row per pipeline stage (enrich, generate, judge, visuals, enrich_conflict, caption_hashtags, context_enrich, composite, publish); stores `stage`, `provider`, `model_name`, `latency_ms`, `tokens_in`, `tokens_out`, `cost_usd`, `ok`, `detail`, `score`. `provider == "nvidia"` StageEvents are what `paid_call_count()` counts toward the budget cap.
- `credentials` — OAuth tokens for publish-target accounts; `token_blob` and `refresh_token_blob` are encrypted at rest via `Encrypted` TypeDecorator (auto-decrypted on read — code sees plain strings); `provider_account_id` holds a provider-specific ID discovered during OAuth (e.g. the Instagram Business Account ID behind a connected Facebook Page); `provider` is `"youtube"` or `"instagram"` (not the `CutPlatform` value — see `credential_provider_for_platform()`)
- `performance_notes` — standalone table (no FK), operator-written plain-English notes on past reel performance; `active` (default `true`) gates whether a note is seeded into `generate_guide`'s standard-path `prior_feedback`. Deliberately not automated few-shot injection of raw past-reel content — see `docs/specs/2026-09-phase5-quality-engagement-feedback.md` §3.1. CRUD is hard-delete (cheap, operator-owned free text, unlike a `Job`/`Cut` state machine).

Video files live on disk (`VIDEO_STORE_DIR`); Wikipedia images in `ASSET_STORE_DIR/wiki/`; TTS audio in `ASSET_STORE_DIR/tts/`; DB stores paths only.

`cuts.guide` is the full serialized `PlatformGuide` dict. Always deserialize with `PlatformGuide(**cut.guide)` before using.

## Key conventions

- **Routers return HTML, not JSON** (except `GET /api/jobs/{id}`). Use `response_class=HTMLResponse` and `templates.TemplateResponse(...)`.
- **Every Job-backed task** (`enrich_context`, `generate_guide`, `render_cut`, `publish_cut`; not the beat tasks `reap_stuck_jobs`/`pull_publish_metrics`) is `@celery_app.task(bind=True, max_retries=n)` over `@job_task("<job type>", prepare=..., after_commit=..., after_commit_failed=...)` from `worker/tasks/common.py`, wrapping a body `(self, db, job, ctx)`. The decorator owns the claim, heartbeat, done-stamp, retry-reset and failure stamp; the body calls `heartbeat()` for progress and raises to fail.
  - Order: `status != pending` → return; atomic claim (`pending → running`, losing the race returns); heartbeat thread starts; `prepare(db, job) -> ctx`; `attempts` bump + `started_at`; body; fenced done-stamp; `after_commit(result)`.
  - `prepare` runs after the claim but **before** `attempts`/`started_at` are set: row loads, `... no longer exists` guards and budget checks go here, so a failure there fails the job without bumping `attempts`.
  - The body must **not commit after its last domain mutation** — the done-stamp commit lands it atomically with `status = done`. `record_stage()`/`heartbeat()` commit, so keep them before the final mutations. The one deliberate exception is an irreversible external side effect (`publish_cut` commits `platform_post_id` right after the upload).
  - `after_commit(result)` runs after the done commit (used by `enrich_context` to enqueue `generate_guide`); it receives the body's return value, never a session, and never retries. If it raises, the job is flipped `done → failed` and `after_commit_failed(db, job, result)` cleans up what the body left behind (`enrich_context` fails the orphan generate Job and rolls the reel back from `generating`). That flip commits even if the hook itself raises — it runs inside its own SAVEPOINT (`db.begin_nested()`), so a raise partway through a multi-write hook only undoes the hook's own writes, never the failure stamp. This matters for `_abandon_generate` specifically: it does two sequential writes (fail the orphan Job, then roll the reel back) — without the savepoint, a failure partway through would leave the orphan Job `failed` (invisible to the reaper's pending-job sweep) while the reel stayed stuck in `generating` with no backstop at all.
  - Sessions are opened with `expire_on_commit=False`; the default would leave a transaction idle-in-transaction across every long LLM / ffmpeg / upload call. The done-stamp fence, not fresh reloads, protects against a stale view.
  - Every lifecycle write is a compare-and-set on `Job.status`, so a worker that lost the job (reaped, or claimed by a sibling) never overwrites the winner or rolls back its owner. Errors while recording a failure are logged and never mask the original exception; NUL bytes are stripped from `job.error`. A `SystemExit`/`KeyboardInterrupt` FAILS the job immediately and frees its owner (`_fail_interrupted`) — never hands it back to `pending`, since Celery has already acked or dropped the message by then and nothing would ever pick a pending job back up. This applies to every job type; there is no per-task opt-out. It fires both where Python unwinds directly (solo/threads pools, Ctrl-C) and, via Celery's SIGTERM-then-SIGKILL time-limit escalation, when a hard time limit kills a prefork child — a plain SIGKILL (the documented `pkill` restart landing before that escalation, or a lost race against `_fail_interrupted`'s own DB write) never reaches this code, so the job stays `running` for the reaper to fail after `STALE_MINUTES`; the reaper remains the backstop, not a fallback that's rarely needed. If `_fail_interrupted` itself can't write because ITS connection is the one that died, it retries once on a brand-new session (`_finalize_or_reconnect`) rather than silently doing nothing. A DB error before the claim is retried up to `PRECLAIM_MAX_RETRIES` (3) even for `max_retries=0` tasks, since nothing has run yet; once that budget is used up (or the error isn't transient), the still-pending job is failed and its owner freed rather than left stranded.
  - `del run.__wrapped__` is what keeps `.delay(job_id)` working: `functools.wraps` alone exposes the body's signature to Celery and it raises `TypeError`. `run.job_type` / `run.max_runtime_s` record the wiring for tests.
  - `enrich_context` takes the job row lock (`lock_job`) before touching the reel: the reaper locks job then owner, and the reverse order deadlocks.
  - `heartbeat()` is fenced: it raises `JobLost` when the job is no longer `running` (the reaper gave up on it), aborting a zombie body at its next milestone. It COMMITS, so call it before the body's last mutations.
  - A refused retry message (`Reject`, broker down) fails the job at once, whether or not this run ever claimed it — no message is coming back for it either way.
  - `job.error` is sanitised (`_error_text`): NUL bytes removed and lone surrogates replaced with `?` (Postgres rejects both, which would fail the commit that records the failure) and SQLAlchemy's `[parameters: …]` redacted (they can echo bound tokens).
  - Failure rolls back the owner via `api/state.py::JOB_IN_FLIGHT` — only the state that job type owns (a stale render job never flips a `publishing` cut); the reaper uses the same table. `guide_ready`/`in_review` are deliberately absent.
- **Idempotency guard**: see the atomic claim above. A `failed` job never re-runs: a late redelivery of a job the reaper already failed would otherwise (for publish) upload the video.
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
- **Performance-note seeding into `prior_feedback` has two specific correctness traps** (`worker/tasks/generate.py::generate_guide`) — both caught by dedicated regression tests before this shipped, not hypothetical:
  1. `active_notes_rows = db.query(models.PerformanceNote).filter(active.is_(True)).all()` is queried **unconditionally at the top of the function**, before the structured-vs-standard branch — never inside the `if guide is None:` (standard-path-only) block. The shared `job.meta = {..., "quality_score": ..., "performance_note_ids": [...]}` write at the end of the function is reached by **both** paths; querying it in the wrong branch is a `NameError`/`UnboundLocalError` on every structured-path success, and a standard-path-only smoke test would never catch it.
  2. `feedback` is seeded from `active_notes` **before** attempt 1 (`feedback: list[str] = list(active_notes)`, not `[]`), so the existing per-attempt line **must be additive**: `feedback = active_notes + [i for i in last_issues if not i.startswith("Score breakdown")]`, never a plain replace. A plain replace was correct before this feature existed (nothing to lose from an empty starting list) but silently drops the seeded notes on attempt 2 and 3 once seeding is added.
- **Active `PerformanceNote`s only reach the standard LLM path** — the structured-script path never calls `build_messages()` (only `build_visuals_messages()`, which has no `prior_feedback`-equivalent parameter), so notes have no effect there. Both paths' `score_guide()` calls DO get `axis_multipliers` (that's the scorer, not a prompt).
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
- **Video streaming path guard**: `stream_video` validates `cut.video_path` is under `VIDEO_STORE_DIR` via `Path.resolve().is_relative_to()` before serving — never bypass this. `stream_thumbnail` applies the identical guard to `cut.thumbnail_candidates[index]`, and `stream_subtitles` (Phase 5d) applies the identical guard to `cut.subtitle_path`.
- **Caption/subtitle offset handling — `.words`/`.segments` must both stay beat-relative at the source, always** (Phase 5d): `engine/render/captions.py::transcribe_audio()` returns a `TranscriptResult` with `.words` (the pre-existing burned-in-text input) and `.segments` (new, feeds SRT export) from one Whisper pass. Both fields are computed with `beat_offset_s` staying at its default `0.0` — `engine/render/compositor.py::_build_beat_transcripts()` is the one call site and never passes a non-zero value. Why this matters: `compositor.py::_whisper_timestamps()` already adds each beat's cumulative start time to `.words` to convert beat-relative → absolute for the drawtext overlay. If a caller pre-shifted `.words` (or `.segments`) by passing a non-zero `beat_offset_s` into `transcribe_audio()`, that addition would double-apply and silently corrupt caption timing for every beat past the first — a real bug the first draft of this feature's design introduced and an adversarial design review caught before any code was written (see `docs/specs/2026-09-srt-caption-export-system-design.md` §2's "Revision note"). `composite_cut()` instead shifts `.segments` to the reel's absolute timeline itself, at the SRT-cue-building call site, via an **explicit running sum over `beat_durations`** — deliberately not the first loop's `t` variable, which has already run to completion (and holds only the reel's total duration) by the time that step runs. Two dedicated, independently mutation-tested regression tests guard this in `tests/test_audio_text_sync.py`: one asserts `transcribe_audio()` is always called with `beat_offset_s=0.0` for every beat, the other re-derives that `_whisper_timestamps()`'s resulting absolute times for a multi-beat reel are not doubled.
- **`record_stage()` composition for a "never fail the job, but still tell the truth" side effect** (Phase 5d, `engine/publish/youtube.py::YouTubePublisher.publish()`'s best-effort captions upload is the concrete example — generalize this pattern to any future best-effort step wrapped in `record_stage`): `engine/observability.py::record_stage()` does **not** swallow exceptions — it sets `ev.ok = False` and commits a `StageEvent` on an exception raised inside its `with` block, but then **re-raises**. A step that must never fail its enclosing job (the video is already live; a captions-only failure surfacing as "publish failed" would be actively wrong) cannot simply wrap the risky call in `record_stage(...)` with no inner handling — the re-raise would still fail the job. It also cannot catch the exception *outside* the `with` block, or *inside* it without touching `ev` — either way leaves `ev.ok` at its default `True`, silently recording a failed call as successful and losing the only operator-visible signal that it didn't happen. The one composition that satisfies both requirements: put the `try`/`except` **inside** the `with record_stage(...) as ev:` block, and on exception explicitly set `ev.ok = False` and `ev.detail["error"] = repr(exc)` before letting the function return normally. Both wrong compositions were mutation-tested during implementation (temporarily reintroduced, confirmed the dedicated `ok=False`-assertion test in `tests/test_tasks_real_db.py` fails for each, then reverted) — this is not a hypothetical concern, it's the design's own documented "must-fix" finding from its adversarial review pass.
- **Hook variants and thumbnail candidates are generated once per reel/render, not tunable per request**: `generate_hook_variants()` runs a single best-effort call after the guide already cleared the quality gate — it never fails the generate job, and the same variant list is copied onto every cut of the reel (platform guides normally share identical beats). `render_cut` always writes all 4 thumbnail candidates; there's no config to change the count or sample points other than editing `engine/render/compositor.py::_THUMBNAIL_CANDIDATE_FRACTIONS`. Both `POST /cuts/{id}/hook-variant` and `POST /cuts/{id}/thumbnail` are gated to `in_review` only, same as the PATCH beat-edit endpoint — a re-render replaces `thumbnail_candidates` wholesale (any prior operator pick is lost, same as `video_path`).
- **safe_to_publish is enforced exactly once**: `engine/publish/gate.py::assert_safe_to_publish()`, called in `publish_cut` immediately before any credential lookup or upload. It guards what goes OUT to a platform, so the finalize path (a cut that already has a `platform_post_id`, which uploads nothing) is not gated: blocking it would leave a live post unrecorded with no operator way out. Nowhere else checks it — computing the field (asset_sourcer.py) is not the same as enforcing it.
- **Publish-time video/pins staleness gate — fingerprint comparison, not eager `video_path` clearing** (Phase 7e): `CutAsset` pins commit incrementally per beat inside `render_cut`'s beat loop (`resolve_or_reuse()`, deliberately — a crash mid-resolve leaves the old pin in place rather than an uncommitted gap), while `Cut.video_path`/`thumbnail_path`/`duration_s` are set only once, at the very end of a successful render. A render that re-pins a beat (guide edit changed `visual_direction`) and then fails anywhere after that pin commit but before the final assignment leaves the database with `CutAsset` reflecting the *new* resolution while `video_path` still points at the *old* file — and `assert_safe_to_publish()`, which only ever reads current `CutAsset` rows, has no way to know they no longer describe the file about to ship. The fix is `engine/render/asset_sourcer.py::compute_pins_fingerprint(db, cut_id) -> str | None` — a deterministic sha256 over sorted `(beat_index, order_in_beat, asset_id)` tuples, `None` when the cut has zero bound pins — computed twice: once by `render_cut` at the exact point `video_path` is set (writing `Cut.rendered_pins_fingerprint`), and once by `engine/publish/gate.py::assert_video_matches_pins(db, cut)` at publish time, which raises `ValueError` on a mismatch. Wired into `publish_cut` in the same branch and at the same timing as the existing `assert_safe_to_publish(db, cut.id)` call — never on the `if cut.platform_post_id:` finalize branch, which uploads nothing and would leave a live post unrecorded with no operator way out if gated. **Why a fingerprint and not just clearing `video_path` on every render/re-pin**: eager-clearing would destroy a perfectly good, already-approved video on every re-render, including one that fails for a reason that has nothing to do with asset safety (a transient TTS network hiccup, an unrelated ffmpeg crash) — a real UX regression bundled into a safety fix. **Why `rendered_pins_fingerprint is None` is treated as "unknown, don't block" rather than a mismatch**: every `Cut` row that predates migration `0012` has `rendered_pins_fingerprint = NULL` while its live pins hash to something real and non-null — a naive "any mismatch blocks" implementation would immediately block publishing on every existing rendered-but-unpublished cut in the database the moment this shipped, a false-positive trap for a fix whose entire point is narrowing a safety gate, not widening it. This is deliberately **not** backfilled: a backfill computed from a cut's *current* pins would be accurate for every cut the bug never hit, but would actively paper over the one class of cut this fix exists to catch (a cut where the bug has already silently caused a mismatch) by blessing its already-wrong state as "matching." The gap self-heals the moment an affected cut is next re-rendered — every future publish attempt on it is protected from then on. See `docs/specs/2026-09-video-pins-staleness-gate-system-design.md` §2 and §7 for the full reasoning, and `tests/test_publish_gate.py::test_none_fingerprint_does_not_block_even_with_real_current_pins` (mutation-tested: making the check fire on `None` too breaks this specific test) for the regression guard.
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
- **Phase 5** ✅ — Analytics: local-library music mixing with sidechain ducking (`LocalMusicSource` + `_build_ffmpeg_args`), HF asset-generation cost tracking (`engine/render/pricing.py`, `asset_hf_video`/`asset_hf_image` StageEvents), Wikipedia attribution block appended to published captions (`engine/publish/attribution.py`), post-publish metrics pull-back every 6h (`worker/tasks/metrics.py`), quality-vs-views surfaced on the reel list and per-cut engagement stats on the cut card. Quality↔engagement correlation (`engine/analytics/correlation.py`, `GET /api/insights`) and performance-informed feedback (`PerformanceNote` CRUD + seeding into `prior_feedback`, `evaluator_axis_weight_multipliers` lever) shipped after — see below. Word-level SRT caption export (item 5d) shipped last, after 5g — see its own roadmap entry and the `docs/roadmap.md` "5d" section for the offset-handling and `record_stage` composition rules it introduced.
- **Phase 5g** ✅ — Quality↔engagement correlation + performance-informed feedback: closes the last two Phase 5 items (`docs/specs/2026-09-phase5-quality-engagement-feedback.md`). `GET /api/insights` shows a Pearson `r` (+ `sample_size`, always shown together, never `r` alone) between `quality_score` and max per-reel `views`, refusing to compute below `MIN_SAMPLE=5` reels or on zero variance in either series — with an explicit, permanent UI caveat about restriction-of-range bias (scores cluster near the acceptance threshold by construction) and "correlation, not causation." Same page shows a top/bottom-3 performer table (combined into one list when `n < 6`) with each performer's hook line, for a human operator to write `PerformanceNote`s from — **not** automatic few-shot injection of raw past-reel content, a deliberate scope decision (see the spec's §3.1: this codebase has already been burned by topic-drift from unconstrained prior context leaking into generation). Every active note is seeded into the standard LLM path's `prior_feedback` from attempt 1 onward. `evaluator_axis_weight_multipliers` (`Settings`, default `{}`) is a manual per-axis scoring lever informed by the correlation data — no code in this repo derives these values statistically.
- **Phase 6 (partial)** — Creative range: hook/thumbnail variant generation, per-reel TTS voice choice, non-football niche evaluator fairness fixes, and per-reel text color. `render_cut` samples 4 thumbnail candidates per render (`Cut.thumbnail_candidates`); `generate_guide` generates 3 alternate hook lines once per accepted guide (`Cut.hook_variants`, best-effort — never fails the job). Operator picks either from the `in_review` cut card (`POST /cuts/{id}/thumbnail`, `POST /cuts/{id}/hook-variant`). `Reel.tts_voice` (create-reel form, curated edge-tts voice list) lets each reel sound different — edge provider only, see Key conventions. Investigating the evaluator's "universal" niche vocabulary found 2 real fairness bugs (Script→Visual Alignment collapsing to a flat max deduction, Visual Variety being unconditionally football-only with no niche gate at all) — both fixed, see the `evaluator.py` module-layout entry and `docs/roadmap.md` Phase 6c. `Reel.text_color` (create-reel form, curated color list) is the first slice of brand customization (Phase 6d) — done as a security-validated lever (unescaped ffmpeg filter interpolation), not just a UX one. Not done: logo/watermark overlay, per-channel presets.
- **Phase 7 (partial)** — Production hardening: `asset_sourcer` black-frame visibility, and a real Dockerfile/deploy path. `render_cut` now tracks which beats got no real media anywhere in the Wikipedia → Pexels → HF Video → HF Image fallback chain and writes the list to `Cut.black_frame_beat_indices` (migration `0010`), surfaced as a warning banner on the `in_review`+ cut card. The sourcers themselves still swallow exceptions and degrade silently *internally* — this only stops the *result* from being silent to the operator; a real retry/alerting fix is a separate, larger design pass. `Dockerfile` (one image, ffmpeg + edge-tts installed, non-root user) is run four ways via `docker-compose.yml`'s `api`/`worker-generation`/`worker-rendering`/`beat` services and command overrides — migrations are a deliberate one-off (`docker compose run --rm api alembic upgrade head`), never baked into a container's own startup (N worker replicas would race it). Verified end-to-end (full stack up, real migration chain, `GET /` 200 from inside the container, both workers registered all 6 tasks), not just `docker build` — see `docs/roadmap.md` Phase 7b. CI gained a `docker-build` job. `engine/generation/llm.py::validate_configured_models()` (called from `api/main.py`'s `lifespan` hook, Phase 7c) pings both configured LLM models' `{base_url}/models` at startup and warns per misconfigured model — this is exactly the check that would have caught the NVIDIA model-catalog-drift incident proactively instead of after every job failed individually; never blocks startup on a failure. Verified against a real local Ollama instance, not just mocks. `tests/test_golden_reel.py` (Phase 7d) is one mocks-free test — real edge-tts synthesis, real ffmpeg — that runs the TTS→compositor→ffmpeg chain end to end; marked `golden` and excluded from the default `pytest` run (real network call, ~20s) but run explicitly by CI's new `golden-reel` job on every push/PR. Mutation-verified against the actual historical zero-audio bug, not a synthetic stand-in for it. `Cut.rendered_pins_fingerprint` (migration `0012`, Phase 7e) closes the "`safe_to_publish` gate checks the current pins, not the video that will ship" Open Issues item: `render_cut` snapshots a fingerprint of the pins that built the currently-stored `video_path`; `engine/publish/gate.py::assert_video_matches_pins()` recomputes it at publish time and blocks with an actionable message on mismatch (a re-render that re-pinned an asset and then failed before `video_path` caught up), wired alongside the existing `assert_safe_to_publish()` call. `rendered_pins_fingerprint is None` (pre-migration rows) is a deliberate rollout-safety skip, not a gap — see Key conventions and `docs/roadmap.md` Phase 7e. Not done: auth/rate-limiting scope decision.

## Worker queues

- **`generation` queue** — `generate_guide`, `enrich_context`, `publish_cut`, `pull_publish_metrics`, `reap_stuck_jobs`. I/O-bound. Run with `--concurrency=4`.
- **`rendering` queue** — `render_cut`. CPU-bound. Run with `--concurrency=1`. Restarts after 10 tasks (`worker_max_tasks_per_child=10`) to prevent ffmpeg handle leaks.
- **Celery beat** — runs `reap_stuck_jobs` every 60 s and `pull_publish_metrics` every 6 h. Start with `celery -A worker.celery_app beat`.

## What is not yet wired in

- TikTok publishing — `TikTokPublisher.publish()` raises `NotImplementedError` on purpose (see Key conventions). Render/review works; only the upload call is missing.
- "Scheduled" has no trigger UI — the state machine and `publish_cut` both handle a cut already sitting in `scheduled`, but nothing currently transitions a cut *into* `scheduled` (no date/time picker, no Celery-beat-driven scheduled publish).
- Insight enrichment for the **standard LLM path** — currently only applied to structured-script beats
- Multi-image collage within a single beat — currently cycles sequentially; no side-by-side layout
- `PIXABAY_API_KEY` (`pixabay_api_key` in config) — Pixabay's public REST API has never documented a Music endpoint, so this stays unused by design; music sourcing uses `LocalMusicSource` instead (see Key conventions)
- Unpublish / re-publish flows — a published cut has no "take down" or "publish again" action
- Analytics beyond raw views/likes/comments and the quality↔views correlation — no trend charts (`pull_publish_metrics()` still overwrites rather than accumulates a time series), no engagement-rate normalization, no per-axis correlation (which of the 17 evaluator axes individually predicts engagement — `score_guide()`'s internal `deductions` dict has the data but never persists it), no per-niche correlation (one niche run so far), no likes/comments composite metric (views only)
- Automatic/statistical tuning of `evaluator_axis_weight_multipliers` from the correlation data — the multiplier lever exists so a human can act on evidence; fitting it from an n≈10–20 sample would be the same overfitting risk `PerformanceNote`s were designed to avoid on the prompt side

## Docs

- `docs/architecture.md` — system diagram, module breakdown, render pipeline detail
- `docs/data-model.md` — all tables, state machines, guide JSON schema
- `docs/api.md` — all HTTP endpoints, request/response shapes
- `docs/evaluation.md` — two-tier quality scoring, threshold/retry, tuning guide
- `docs/roadmap.md` — phase-by-phase plan with open items and sequencing rationale
