# Architecture

## Overview

Reel Maker is a local-first, single-operator pipeline that turns a text prompt into platform-specific short-form video, carries it through review and editing, publishes it to YouTube/Instagram, and pulls engagement metrics back. The system is designed around a single architectural principle: **every slow operation runs as an async background job**. Nothing slow happens inside a request cycle.

```
┌──────────────────────────────────────────────────────────────────────┐
│                           Browser (HTMX)                             │
│  form submit ──► POST /api/reels                                     │
│  polls every 2s ◄── GET /api/reels/{id}/active-job-fragment          │
│  render button ──► POST /api/cuts/{id}/render                        │
│  polls every 3s ◄── GET /api/cuts/{id}/render-status                 │
│  approve ──► POST /api/cuts/{id}/approve                             │
│  publish button ──► POST /api/cuts/{id}/publish                      │
│  polls every 3s ◄── GET /api/cuts/{id}/publish-status                │
│  connect account ──► GET /api/credentials/{provider}/authorize       │
│  insights / notes ──► GET /api/insights, POST /api/insights/notes    │
└──────────────────────────┬─────────────────────────────────────────┘
                           │ HTTP
┌──────────────────────────▼─────────────────────────────────────────┐
│                      FastAPI (api/)                                 │
│  Routers: reels · jobs · cuts · credentials · insights               │
│  Returns HTML fragments (Jinja2) — only GET /api/jobs/{id} is JSON  │
└────────────┬────────────────────────┬────────────────────────────────┘
             │ SQLAlchemy             │ Celery .delay()
┌────────────▼──────────┐  ┌──────────▼────────────────────────────────┐
│   PostgreSQL           │  │   Redis (broker + backend)                │
│   reels, cuts,         │◄─┤   visibility_timeout = 7200 s             │
│   cut_metric_snapshots,│  └──────────┬────────────────────────────────┘
│   assets, cut_assets,  │             │ task execution
│   jobs, stage_events,  │  ┌──────────▼────────────────────────────────┐
│   credentials,         │  │   Celery Worker (worker/)                  │
│   performance_notes    │  │   enrich_context      [generation Q]       │
└────────────────────────┘  │   generate_guide      [generation Q]       │
                             │   render_cut          [rendering Q]        │
                             │   publish_cut         [generation Q]       │
                             │   pull_publish_metrics[generation Q, beat] │
                             │   reap_stuck_jobs     [generation Q, beat] │
                             └──┬────────────────┬───────────────────────┘
                                │                │
             ┌──────────────────▼──┐   ┌─────────▼───────────────────────────┐
             │  engine/            │   │  External services                   │
             │  generation/        │   │  Ollama / NVIDIA NIM (LLM, 2 tiers)  │
             │  render/            │   │  Pexels API (stock footage)          │
             │  publish/           │   │  Wikipedia REST (player photos)      │
             │  analytics/         │   │  HuggingFace (LTX-Video / FLUX)      │
             │  observability.py   │   │  Edge TTS / Kokoro (narration)       │
             └──────────────────────┘   │  Whisper (optional, word timing)     │
                                         │  YouTube Data API / Instagram Graph  │
                                         └───────────────────────────────────────┘
```

---

## Request / response pattern

The browser never fetches JSON for its own rendering. Every route except `GET /api/jobs/{id}` returns an HTML fragment (Jinja2 templates); HTMX swaps these into the DOM. This is deliberate and load-bearing — see CLAUDE.md's Key Conventions, "Routers return HTML, not JSON."

**Generation flow:**
1. Form `POST /api/reels` (multipart form data — `context`, `niche`, `voiceover_mode`, `target_length_s`, `generation_path`, `platforms[]`, `tts_voice`, `text_color`)
2. API creates `Reel` + one `Cut` per selected platform + a `Job(type=enrich)` row, transitions the reel to `enriching`, enqueues `enrich_context`, returns `fragments/pipeline_status.html`
3. Fragment polls `GET /api/reels/{id}/active-job-fragment` every 2 s — always returns the currently active job (enrich or generate), so the UI tracks seamlessly through both phases
4. `enrich_context`: scores context (5-axis rule scorer, 0–100); if score < 60 and the script isn't structured, calls the enrichment LLM; stores the result in `reel.enriched_context`; creates + enqueues a `Job(type=generate)`
5. `generate_guide` picks up with `effective_context = reel.enriched_context or reel.context`
6. When the generate job is `done`, the fragment shows "Guide ready · quality score NN/100 · context score NN/100 →"; when `failed`, it shows `job.error`

**Render flow:**
1. Button `POST /api/cuts/{id}/render`
2. API transitions the cut, creates a `Job`, enqueues `render_cut`, returns `fragments/render_status.html`
3. Fragment polls `GET /api/cuts/{id}/render-status?job_id={id}` every 3 s
4. When done, shows the `<video>` player, a thumbnail-candidate picker, a hook-variant picker, the "Download captions (.srt)" link (when `subtitle_path` is set), and **Approve** / **Re-render** buttons

**Review & edit flow:**
1. Cut is `in_review` — the cut card renders as an editable form
2. Operator edits beat fields (duration, visual direction, VO script, on-screen text), caption, hashtags, picks a thumbnail candidate, or swaps in a hook variant
3. `PATCH /api/cuts/{id}` updates `cut.guide`/`caption`/`hashtags` — writes a beat field only when its normalized value actually changed (`engine/generation/guide_edit.py::set_beat_field()`), so an untouched resubmit never spuriously dirties `cut.guide`/its fingerprint
4. Re-render: `resolve_or_reuse()` only re-resolves beats whose `visual_direction` fingerprint changed; others reuse pinned assets
5. Approve: `POST /api/cuts/{id}/approve` transitions the cut to `approved`

**Publish flow:**
1. Button `POST /api/cuts/{id}/publish` (from `approved`, `scheduled`, or `failed`) — creates a `Job(type=publish)`, transitions the cut to `publishing`, enqueues `publish_cut`, returns `fragments/publish_status.html`
2. Fragment polls `GET /api/cuts/{id}/publish-status?job_id={id}` every 3 s
3. `publish_cut` runs three gates in order before any network call: `assert_safe_to_publish()` (every bound asset's `Asset.safe_to_publish`), `assert_video_matches_pins()` (the rendered video's asset pins vs. current `CutAsset` pins), `assert_video_matches_guide()` (the rendered video's guide content vs. current `cut.guide`) — then builds the caption via `build_published_caption()` (appends Wikipedia attribution), dispatches to `engine/publish/registry.py::get_publisher(platform)`, and commits `platform_post_id` the moment the upload is live so a later retry finalizes instead of re-uploading
4. A YouTube publish also makes one best-effort `captions.insert` call with the cut's `.srt` file if `subtitle_path` is set — never fails the publish

**Credentials flow:**
1. `GET /api/credentials` shows connect/disconnect state per provider (`youtube`, `instagram`; `tiktok` shown as not-yet-available)
2. `GET /api/credentials/{provider}/authorize` redirects into that provider's OAuth2 consent screen
3. `GET /api/credentials/{provider}/callback` exchanges the code, discovers the right account (for Instagram: swaps to a long-lived token, then discovers the connected Facebook Page's IG Business Account and its Page access token), stores the (encrypted) token, redirects back to `/api/credentials`
4. Every 6 hours, Celery beat's `pull_publish_metrics` pulls views/likes/comments for every `published` cut whose platform has a `MetricsFetcher`, forward-filling `Cut.views`/`likes`/`comments`/`metrics_updated_at` and appending a raw snapshot row to `cut_metric_snapshots`

**Insights flow:**
1. `GET /api/insights` shows a Pearson correlation between quality score and views (refusing to compute below 5 reels or on zero variance), a top/bottom-3 performer table, and the `PerformanceNote` list/form
2. `POST /api/insights/notes` / `POST /api/insights/notes/{id}/toggle` / `DELETE /api/insights/notes/{id}` all return the same `fragments/performance_notes.html` partial for an htmx swap
3. Every *active* note is seeded into `generate_guide`'s standard-LLM-path `prior_feedback` from attempt 1 onward (structured path never sees it — it never calls `build_messages()`)

---

## Module breakdown

### `api/`

| File | Responsibility |
|---|---|
| `main.py` | FastAPI app factory; mounts static files, Jinja2 templates, all 5 routers; `lifespan` hook runs `validate_configured_models()` at startup (best-effort, never blocks) |
| `config.py` | `pydantic-settings` `Settings` — LLM/NVIDIA/HuggingFace/Pexels keys, `credentials_key`, OAuth client id/secret pairs, `public_base_url`, `music_library_dir`, `max_paid_llm_calls_per_reel`, `evaluator_axis_weight_multipliers` (dict, the first dict-typed setting) |
| `crypto.py` | Fernet `seal()`/`open_()` + `Encrypted` SQLAlchemy TypeDecorator for encrypted columns (`Credential.token_blob`/`refresh_token_blob`) |
| `db.py` | SQLAlchemy engine (`pool_pre_ping=True`, `hide_parameters=True`), `SessionLocal`, `get_db()` FastAPI dependency |
| `enqueue.py` | `enqueue_job(db, job, task, *, what)` — the one router-side add → commit → `task.delay` → unwind-on-failure (`fail_unenqueued` + 503) sequence, used by `create_reel`, `trigger_render`, `trigger_publish` |
| `models.py` | All ORM models and status enums — `Reel`, `Cut`, `CutMetricSnapshot`, `Asset`, `CutAsset`, `Job`, `StageEvent`, `PerformanceNote`, `Credential` |
| `oauth.py` | Generic OAuth2 authorization-code flow; `YouTubeOAuth`, `InstagramOAuth`; `new_state()`/`consume_state()` (process-local CSRF state) |
| `schemas.py` | `JobResponse` — the one JSON endpoint's response shape |
| `state.py` | `REEL_TRANSITIONS`, `CUT_TRANSITIONS` dicts + `transition()` guard; `JOB_IN_FLIGHT` — the owner state each job type rolls back to `failed` |
| `routers/reels.py` | `POST /api/reels`, `POST /api/reels/estimate`, `GET /api/reels`, `GET /api/reels/{id}/active-job-fragment`, `GET /api/reels/{id}` (pipeline cost/latency/quality panel; embeds live render/publish status and failure reasons per cut) |
| `routers/jobs.py` | `GET /api/jobs/{id}` (JSON) — the only JSON endpoint |
| `routers/cuts.py` | Render/PATCH/approve/publish/render-status/publish-status, thumbnail-pick and hook-variant-swap endpoints (both share `_cut_card()` with the lifecycle routes — see Key conventions) |
| `routers/cut_media.py` | Video/subtitle/thumbnail file streams (`GET`, raw bytes, no template) + `_resolve_within_video_store()` — split out of `cuts.py` (CAR candidate 2, `improve-codebase-architecture` review) |
| `routers/credentials.py` | Connect/disconnect UI, OAuth authorize/callback per provider |
| `routers/insights.py` | Quality↔engagement correlation + top/bottom performers + `PerformanceNote` CRUD |

### `worker/`

| File | Responsibility |
|---|---|
| `celery_app.py` | Celery instance; `task_acks_late=True`, `visibility_timeout=7200`, split `generation`/`rendering` queues, beat schedule (`reap_stuck_jobs` every 60 s, `pull_publish_metrics` every 6 h) |
| `tasks/common.py` | `job_task()` — the shared Job lifecycle decorator every task body runs under (atomic claim with fencing-token bump → heartbeat thread → `prepare` → body → fenced done-stamp → `after_commit`; retry-reset or failure stamp + per-job-type owner rollback on error, every write a compare-and-set); `heartbeat()`, `rollback_owner()`, `should_retry()`/`is_transient_error()` |
| `tasks/enrich_context.py` | `enrich_context(job_id)` — `evaluate_context()` → `is_structured()` guard → optional `llm_enrich()` → stores `reel.enriched_context` → creates + enqueues the generate job |
| `tasks/generate.py` | `generate_guide(job_id)` — a short orchestration sequence (Phase 7q decomposition) over `_try_structured_path()` / `_run_standard_path_attempts()` / `_maybe_regenerate_caption_hashtags()` / `_maybe_generate_hook_variants()` / `_persist_guide()`, sharing one `_GenerationContext` dataclass; retries transient failures twice |
| `tasks/render.py` | `render_cut(job_id)` — `resolve_or_reuse()` per beat → `synth_to_budget()` → TTS-accurate timecodes → atomic MP4; writes `thumbnail_candidates`, `black_frame_beat_indices`, `rendered_pins_fingerprint`, `rendered_guide_fingerprint`, `subtitle_path`; retries transient failures twice |
| `tasks/publish.py` | `publish_cut(job_id)` — `assert_safe_to_publish()` → `assert_video_matches_pins()` → `assert_video_matches_guide()` → `build_published_caption()` → dispatch via `engine/publish/registry.py` → commit `platform_post_id` the moment the upload is live; `max_retries=0` (irreversible external side effect) |
| `tasks/metrics.py` | `pull_publish_metrics()` — Celery beat task (every 6 h); pulls views/likes/comments per published cut via `get_metrics_fetcher()`, forward-fills `Cut.*`, appends a `CutMetricSnapshot` row; tolerates per-cut failures |
| `tasks/maintenance.py` | `reap_stuck_jobs()` — Celery beat, every 60 s; fails/resumes stale jobs (see "Reliability" below) via compare-and-set, rolling back only the owner state the job's type owns |

### `engine/`

| File | Responsibility |
|---|---|
| `names.py` | `person_names()` / `first_person_name()` — the one shared person-name extractor — strips leading sentence openers, excludes clubs/leagues/tournaments (stdlib-only; used by `evaluator.py`, `visual_fallback.py` and `asset_sourcer.py`) |
| `observability.py` | `record_stage()` context manager — writes a `StageEvent` row on exit (success or failure); `paid_call_count()`; `latest_quality_scores()` (shared by the reel list/detail pages and `engine/analytics/correlation.py`) |

#### `engine/generation/`

| File | Responsibility |
|---|---|
| `guide_schema.py` | `Beat`, `PlatformGuide`, `MasterGuide` Pydantic models; `compute_guide_fingerprint()` |
| `llm.py` | `LLMProvider` + `OllamaProvider` (OpenAI-compatible, captures token usage); `get_llm_provider()`, `get_enrichment_provider()`, `is_nvidia_generation()`; `validate_configured_models()` — best-effort startup model-availability ping |
| `pricing.py` | `llm_cost_usd()` — NVIDIA per-token cost from `Settings` rates (0 until configured) |
| `estimate.py` | `estimate_generation()` — pre-generation call-count/time/cost estimate sourced from real `StageEvent` history |
| `prompt.py` | `build_messages(prior_feedback=)` — closed-loop retry + seeded `PerformanceNote`s; `build_visuals_messages()` — anchors the LLM to per-beat VO only |
| `script_parser.py` | `parse()`, `is_structured()` (≥3 labelled sections — single source of truth shared with `enrich_context.py`'s guard), `derive_on_screen()` (the single canonical VO→on-screen-text word-wrap implementation) |
| `context_enricher.py` | `evaluate_context()` — 5-axis rule scorer; `llm_enrich()` |
| `evaluator.py` | `score_guide()` — decomposed (Phase 7s) into 17 `_score_<axis>(ctx)` functions in `_AXIS_SCORERS`, sharing one `_ScoringContext`; see `docs/evaluation.md` |
| `llm_judge.py` | `judge_guide()` — LLM semantic judge, 5 dimensions × 0–20 |
| `postprocess.py` | `clean_guide()` — strips label prefixes; derives up to 5 `on_screen_text` segments via `script_parser.derive_on_screen()` |
| `visual_fallback.py` | Fallback `visual_direction` synthesis from VO keywords when the LLM returns a degenerate one |
| `niche.py` | `clean_niche()` / `is_football_niche()` — the shared definition of a football niche (substring match) and prompt-safe niche cleaning, imported by both `evaluator.py` and `beat_enrichment.py` |
| `beat_enrichment.py` | `_enrich_with_insight()` (both generation paths, niche-aware: football vs. generic vocabulary/prompt) / `_make_conflict_stub()` (structured path only) — topic-fenced |
| `guide_edit.py` | `set_beat_field()` / `replace_beat_vo()` — operator-driven guide edits (PATCH beat-edit form, hook-variant swap), extracted from `api/routers/cuts.py` with no HTTP knowledge |
| `hook_variants.py` | `generate_hook_variants()` — one best-effort LLM call for 3 alternate hook lines; `[]` on any failure |

#### `engine/render/`

| File | Responsibility |
|---|---|
| `asset_sourcer.py` | `PexelsVideoSource` + `WikipediaImageSource` (+ license metadata) + `HuggingFaceVideoSource`/`HuggingFaceImageSource`; fallback chain Wikipedia → Pexels → HF Video → HF Image → black frame; `_generate_gated_hf_asset()` — shared gate/record_stage/cost shell for both HF tiers; `resolve_or_reuse()` — per-beat asset pinning; `LocalMusicSource.find()`; `compute_pins_fingerprint()` / `compute_pins_fingerprint_for_render()` |
| `pricing.py` | `hf_image_cost_usd()` / `hf_video_cost_usd()` — HF asset-generation cost (0 until configured) |
| `tts.py` | `EdgeTTSProvider`/`KokoroProvider`/`SilentProvider`; `synthesize()` + `synth_to_budget()` (±25% rate adjustment); `CURATED_EDGE_VOICES` |
| `captions.py` | `transcribe_audio()` — one Whisper pass producing both `.words` (burned-in text) and `.segments` (SRT export); no-op if Whisper isn't installed |
| `srt.py` | `write_srt()` — pure `list[CaptionSegment]` → `.srt` formatter, no ffmpeg/network |
| `compositor.py` | `composite_cut()` — MoviePy stage + FFmpeg drawtext stage, returns `(duration, thumbnail_candidates, subtitle_path)`; reader lifetime managed via `contextlib.ExitStack` (Phase 7r) rather than manual tuple-threading; `_build_collage_clip()` for a 2-item beat; `CURATED_TEXT_COLORS`; music mixing with sidechain ducking; atomic `os.replace()` |

#### `engine/publish/`

| File | Responsibility |
|---|---|
| `base.py` | `Publisher` interface + `PublishResult` dataclass; `publish()` takes an explicit `caption` param |
| `registry.py` | `get_publisher(platform)` / `credential_provider_for_platform(platform)` / `get_metrics_fetcher(platform)` (returns `None`, not a raise, for an unmapped platform) |
| `gate.py` | `assert_safe_to_publish()` / `unsafe_assets()`; `assert_video_matches_pins()`; `assert_video_matches_guide()` — the three publish-time gates |
| `attribution.py` | `build_attribution_block()` / `build_published_caption()` — appends Wikipedia attribution without mutating the DB-stored caption |
| `metrics.py` | `EngagementMetrics` + `MetricsFetcher` base; `YouTubeMetricsFetcher`, `InstagramMetricsFetcher` (with metric-name-drift detection via `record_stage`) |
| `youtube.py` | `YouTubePublisher` — resumable upload; best-effort `captions.insert` when `subtitle_path` is set; `get_valid_access_token()` |
| `instagram.py` | `InstagramPublisher` — container create/poll/publish (Reels); requires `public_base_url` |
| `tiktok.py` | `TikTokPublisher` — deliberately `NotImplementedError` (Content Posting API needs a separate audited app review) |

#### `engine/analytics/`

| File | Responsibility |
|---|---|
| `correlation.py` | `quality_engagement_correlation()` — Pearson `r` + sample size, refusing below `MIN_SAMPLE=5` or on zero variance; `top_bottom_performers()` — top/bottom-3 (or one combined list below `2k`) |

### `tests/`

54 test files + `conftest.py`, **1026 tests run by default / 1027 total** (1 golden-reel test, marked `golden`, is deselected by default — real edge-tts + real ffmpeg, ~20 s, run explicitly by CI). Counts below are what `pytest --collect-only` actually reports for each file today:

| File | Tests | File | Tests |
|---|---|---|---|
| `test_job_lifecycle.py` | 133 | `test_evaluator.py` | 47 |
| `test_maintenance.py` | 52 | `test_reels_router.py` | 38 |
| `test_r3_proposed.py` | 38 | `test_cuts_publish_router.py` | 34 |
| `test_compositor.py` | 30 | `test_r4_gaps.py` | 30 |
| `test_generate_task.py` | 26 | `test_common.py` | 18 |
| `test_audio_text_sync.py` | 16 | `test_guide_edit.py` | 16 |
| `test_render_task.py` | 16 | `test_tts.py` | 16 |
| `test_asset_sourcer_cost.py` | 15 | `test_enrichment.py` | 38 |
| `test_state.py` | 15 | `test_variants_router.py` | 8 |
| `test_asset_sourcer.py` | 14 | `test_context_enricher.py` | 14 |
| `test_enrich_context_task.py` | 14 | `test_oauth.py` | 14 |
| `test_publish_task.py` | 14 | `test_llm_provider.py` | 13 |
| `test_publish_gate.py` | 13 | `test_correlation.py` | 12 |
| `test_metrics_fetcher.py` | 12 | `test_script_parser.py` | 11 |
| `test_metrics_task.py` | 10 | `test_credentials_router.py` | 9 |
| `test_hook_variants.py` | 9 | `test_insights_router.py` | 9 |
| `test_youtube_publisher.py` | 9 | `test_attribution.py` | 8 |
| `test_estimate.py` | 8 | `test_music_source.py` | 8 |
| `test_srt.py` | 8 | `test_guide_schema.py` | 7 |
| `test_instagram_publisher.py` | 7 | `test_publish_registry.py` | 7 |
| `test_tasks_real_db.py` | 7 | `test_wikipedia_image_source.py` | 6 |
| `test_niche.py` | 22 | `test_cut_media.py` | 14 |
| `test_names.py` | 109 | `test_visual_fallback.py` | 17 |
| `test_asset_sourcer_names.py` | 7 | `test_enqueue.py` | 13 |
| `test_pricing.py` | 4 | `test_config.py` | 2 |
| `test_llm_judge.py` | 3 | `test_main.py` | 3 |
| `test_observability.py` | 3 | `test_golden_reel.py` | 1 (deselected) |

Notable areas of coverage beyond what the filenames suggest: `test_job_lifecycle.py` and `test_maintenance.py` together carry the Job-fencing-token mechanism's regression suite (zombie resumed-while-still-alive detection at every `heartbeat()`/`lock_job()`/done-stamp/CAS checkpoint, differentiated per-job-type resume budgets); `test_compositor.py` covers the `ExitStack` reader-lifetime migration and the 2-up collage layout with real ffmpeg frame sampling; `test_evaluator.py` covers all 17 per-axis scorer functions in isolation; `test_publish_gate.py`/`test_cuts_publish_router.py` cover both staleness gates end-to-end.

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

### `generate_guide()` orchestration (Phase 7q decomposition)

`generate_guide()` is no longer one long function — it builds one `_GenerationContext` (13 read-only values every section needs: db, job, reel, cuts, platforms, target lengths, effective context, active `PerformanceNote`s, axis multipliers, quality threshold, llm) and runs a short sequence:

```
_strip_stale_fallback_meta()
  → _load_active_performance_notes()
  → _try_structured_path(ctx, stubs)       # falls back to:
  → _run_standard_path_attempts(ctx, stubs)
  → _maybe_regenerate_caption_hashtags(ctx, guide)   # standard path only
  → _maybe_generate_hook_variants(ctx, guide)
  → _persist_guide(cuts, guide, hook_variants)
  → transition + final job.meta write
```

Both path-runners return a `GuideResult(guide, score, issues, exc)` NamedTuple. This is a pure refactor — see CLAUDE.md's Key Conventions entry for the mutation-testing and 4-persona-review history behind it.

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

**Visual fallback**: `_is_degenerate_visual()` catches empty or boilerplate visuals. `_fallback_visual()` maps VO keywords to specific shot types.

**Quality gate**: if `score_guide()` on the structured-path guide falls below the quality threshold, generation falls through to the standard LLM path (`job.meta["structured_fallback"] = True`, `job.meta["structured_score"]` records the discarded score).

### Standard LLM path (2–5 min)

```
context + prior_feedback (active PerformanceNotes, then + prior issues on retry)
  └── build_messages(prior_feedback=issues)
        └── main LLM (full MasterGuide JSON)
              └── MasterGuide.model_validate_json()
                    ├── clean_guide()
                    └── score_guide() + judge_guide()   # two-tier eval
                          ├── record_stage("generate")
                          ├── record_stage("judge")
                          ├── combined ≥ threshold → accept   (80 NVIDIA / 65 local Ollama)
                          ├── combined < threshold → feedback = active_notes + issues; retry (up to 3×)
                          └── if all 3 fail → accept best-of-3
```

**Closed-loop retry**: failure issues from `score_guide()` + `judge_guide()` are appended to `active_notes` (never a plain replace — see CLAUDE.md's Key Conventions) as a second user message in the next attempt. If no attempt clears the threshold, the highest-scoring guide is accepted rather than failing the job.

**Caption/hashtag regeneration**: after a standard-path guide is accepted, one best-effort extra LLM call regenerates `caption`/`hashtags` from the guide's real VO content (`_generate_caption_hashtags()`, shared with the structured path), overwriting every platform guide's caption/hashtags on success and leaving the model-generated values untouched on failure — same paid-call-budget gate as hook variants, skipped outright when every beat's `vo_script` is empty (`music_only`/`silent` mode).

### Two-tier quality evaluation

```
score_guide()     deterministic, 17 axes, decomposed into _score_<axis>(ctx) functions
                   (max deductions exceed 100; score clamped 0–100):
  Retention Architecture  20 pts   (hook strength + open loops + momentum shifts)
  Narrative Quality       15 pts   (HOOK→CONTEXT→ANALYSIS→CONFLICT→CONCLUSION arc)
  Context Coverage        10 pts   (≥50% of source sentences echoed in VO)
  Insight Density         15 pts   (stats, causal language, tactical terms, comparisons)
  Script↔Visual Align     20 pts   (entity + action + context match, redistributed when a
                                    sub-signal doesn't apply to the niche)
  Clip Availability       10 pts   (visual_direction describes sourceable footage)
  Visual Editability      10 pts   (specific enough for automation)
  Emotional Impact        13 pts   (density 10 + distribution across beats 3)
  Audio Delivery          10 pts   (WPS range, hook capped tighter + sentence length + rhythm)
  Visual Variety           5 pts   (mix of shot types; skipped when every beat is uncategorized)
  Duration Fit             5 pts   (beat durations within ±30% of target, per cut)
  Caption & Hashtag        5 pts   (caption ≥30 chars, ≥10 hashtags, per cut)
  CTA Action               3 pts   (quality-weighted: prediction/opinion > passive follow)
  Conversational Tone     10 pts   (penalise encyclopaedic phrasing; reward direct address)
  Hook-CTA Throughline     5 pts   (CTA references the hook's tension or player)
  Per-Beat Specificity     5 pts   (each body beat makes a falsifiable claim)
  Repetition               5 pts   (body beats use distinct vocabulary)

  Beat-level axes de-duplicate beats across guide.cuts by
  (index, vo_script, visual_direction) — both platform guides normally hold
  identical beats. Per-cut axes (duration, caption, hashtags) deduct once per
  platform by design. `evaluator_axis_weight_multipliers` scales a named axis's
  deduction after every axis function has run, via one small correction block.

  if rule ≥ 55:
    judge_guide()  LLM semantic, 5 dimensions × 0–20:
      Factual accuracy · Expertise depth · Natural speech
      Hallucination risk · Shareability

combined = int(rule × 0.4 + llm × 0.6)   threshold 80 (NVIDIA) / 65 (local Ollama)
```

See `docs/evaluation.md` for the full tuning guide.

---

## Reliability

### Celery configuration (`worker/celery_app.py`)

| Setting | Value | Reason |
|---|---|---|
| `task_acks_late` | `True` | Ack only after task returns — killed worker requeues (the redelivered message finds the job `running`/`failed` and no-ops; recovery is the reaper, then an operator retry) |
| `task_reject_on_worker_lost` | `True` | SIGKILL redelivers the message rather than dropping it |
| `broker_transport_options.visibility_timeout` | 7200 s | Outlasts normal broker/worker hiccups; a redelivery of a still-running job is a safe no-op via the atomic claim |
| `worker_prefetch_multiplier` | 1 | No worker hoards multiple long tasks |
| `worker_max_tasks_per_child` | 10 | Respawn render workers to reclaim MoviePy/ffmpeg memory |
| `max_retries` | 2 (via `should_retry()`) | 30 s/60 s backoff on transient failures only (`generate_guide`/`render_cut`); `enrich_context`/`publish_cut` stay at 0 — enrichment retry re-runs a paid call for no gain, and a publish retry after an accepted upload would post twice |

### Idempotency and the fencing token

`job_task` lets only a `pending` job run; `done`/`running` are redelivery no-ops, and `failed` is terminal (an operator retry creates a new Job). The claim is an atomic `UPDATE … WHERE status = 'pending'`, bumping `Job.claim_token` (a monotonic fencing counter, migration `0013`).

**Why a token, not just status**: `reap_stuck_jobs` can resume a `running`-stale job of a resumable type (`enrich`/`render`/`generate` — never `publish`) back to `pending` for a second worker to claim, instead of only ever failing it. Once that's possible, `status='running'` alone can no longer distinguish "the run that currently owns this row" from "a run that used to" — a live-but-falsely-detected-stale zombie's own `heartbeat()`/`lock_job()` calls would otherwise still pass. Six checkpoints carry the captured token (`heartbeat()`, `lock_job()`, the done-stamp CAS, `_settle_failure`'s retry-reset and fail-CAS, `_heartbeat_loop`'s own periodic write, and `_fail_interrupted`'s shutdown path) so a superseded run is fenced off at its very next checkpoint instead of corrupting the resumed run's result. `generate`'s own resume budget is capped at 1 (not 2, like `enrich`/`render`) because a killed standard-path attempt has already durably spent part of the reel's paid-call budget. `publish` is structurally excluded (never resumed — an irreversible external side effect) via both dict omission and a module-level assert. See CLAUDE.md's Key Conventions entry for the full two-round review history behind this mechanism.

### Heartbeat + stuck-job reaper

`job_task` refreshes `job.heartbeat_at` every 30 s from a background thread while the body runs, and bodies call `heartbeat(db, job, progress)` for progress. `reap_stuck_jobs` (Celery beat, 60 s interval) fails or resumes three kinds of stalled job:

- **`running`, stale heartbeat** (> 5 min, `STALE_MINUTES`) — worker killed mid-task, or a body past its `max_runtime_s`. A resumable type under budget goes back to `pending` and is re-enqueued; otherwise it's failed.
- **`pending`, stale `updated_at`** (> 240 min, `PENDING_STALE_MINUTES`) — never picked up at all. Deliberately long: render runs at concurrency 1 and can take an hour; generation slots can be busy for hours. Keyed on `updated_at`, not `created_at`, so retry backoff isn't mistaken for staleness.
- **`done`, no error, owner still mid-flight** (> 15 min, `DONE_ORPHAN_STALE_MINUTES`) — `job_task`'s own `after_commit_failed` cleanup hook hit a compound failure recording its own recovery, so the fail-stamp that should have flipped the Job to `failed` never landed. This sweep is the backstop for that narrow gap.

Each reap is a compare-and-set that re-checks staleness in the UPDATE, and rolls back only the owner state the job's type owns (`JOB_IN_FLIGHT`). The task is routed to the `generation` queue with a 55 s message expiry so a starved queue doesn't drain a backlog in a burst.

### Atomic file writes

The compositor writes FFmpeg output to `{out}.tmp.mp4`, then `os.replace()` to the final path. A killed process never leaves a servable half-written video. The same rule applies to every asset download (Pexels, Wikipedia, both HuggingFace sources) and to `EdgeTTSProvider.synthesize()`.

**TTS duration override caveat:** `render_cut` replaces each beat's `duration_s` with the measured audio length, but skips this when the active provider is `SilentProvider`.

---

## Observability

`engine/observability.py` provides `record_stage()` — a context manager that writes a `StageEvent` row on exit (success or failure), and `paid_call_count()` / `latest_quality_scores()` as shared helpers.

```python
with record_stage(db, reel_id, "judge", provider="nvidia", model=MODEL, attempt=1) as ev:
    score, reasons = judge_guide(context, guide, llm)
    ev.score = score
    ev.detail["reasons"] = reasons
```

Stages wired today: `context_enrich`, `enrich`, `enrich_conflict`, `visuals`, `generate` (each retry), `judge` (each retry), `caption_hashtags`, `composite`, `asset_hf_video`, `asset_hf_image`, `publish`, `captions_upload`, `instagram_metrics`.

`StageEvent` fields: `stage`, `provider`, `model_name`, `latency_ms`, `tokens_in`, `tokens_out`, `cost_usd`, `attempt`, `score`, `ok`, `detail` (JSON), `created_at`. `cost_usd` **is** populated — for NVIDIA LLM calls (`engine/generation/pricing.py`) and HF asset generation (`engine/render/pricing.py`) — both `0.0` until the operator sets a real per-unit rate; `asset_hf_*` stages only charge `cost_usd` on an actual generation call, never a cache hit.

The `/api/reels/{id}` pipeline panel sums `cost_usd`/`latency_ms` across stages, **excluding** `instagram_metrics` from the headline totals (it fires every 6 h for as long as a cut stays published — unbounded, unlike every generation/render stage) while still giving it its own row in the per-stage breakdown table.

---

## Per-beat asset pinning

`resolve_or_reuse()` in `asset_sourcer.py` provides deterministic re-renders:

1. Computes `fingerprint = sha256(visual_direction)[:16]`
2. Queries `cut_assets` for existing pins for this `(cut_id, beat_index)`
3. If all pins have matching `resolved_from`: returns pinned assets directly (no API call)
4. Otherwise: calls `resolve_beat_assets()`, deletes stale pins, inserts new ones with the fingerprint

This means editing beat 3's VO and re-rendering only triggers a new API call for beat 3; all other beats reuse cached pins. `compute_pins_fingerprint_for_render()` hashes the full set of currently-bound pins and is snapshotted onto `Cut.rendered_pins_fingerprint` at render success (see "Publishing" below).

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
              └──► failed ──► draft (render retry) / approved (publish retry)
```

`"failed"` is deliberately ambiguous — it covers both a failed render and a failed publish; which transition applies is decided by which endpoint the operator hits, not tracked on the Cut itself.

---

## Render pipeline detail

`composite_cut()` runs two stages inside one `contextlib.ExitStack` (Phase 7r — every real `VideoFileClip`/`AudioFileClip` reader is registered the moment it's opened, closed automatically on any exit path at any nesting depth):

**Stage 1 — MoviePy (video + audio, no text):**
```
For each beat:
  resolve_or_reuse() → media file(s) for this beat
  synth_to_budget(vo_script, target_s=beat.duration_s) → .mp3
    ↳ adjusts speaking rate ±25% if measured duration drifts > 15%

  1 item  → Ken Burns sub-clip (image) or scale/crop/loop (video)
  2 items → side-by-side collage, each half its own Ken Burns crop of the full beat
            duration (_build_collage_clip() — e.g. two Wikipedia player headshots
            named in the same beat)
  3+ items → sequential Ken Burns sub-clips, duration split across items

  concatenate sub-clips → beat_clip
  VO audio: AudioFileClip → subclip if too long → .with_effects([AudioFadeIn(0.12), AudioFadeOut(0.12)]) → .with_start(t)

After all beats:
  concatenate beats → final video
  position each VO audio at beat offset → CompositeAudioClip
  write 4 thumbnail candidate frames across the reel → thumbnail_candidates
  build SRT cues from Whisper .segments (or proportional fallback) → subtitle_path
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

Every drawtext clause carries `:expansion=none` — disables ffmpeg's own `%`-expansion engine wholesale, since the previous `\%` escape for a literal percent sign was never a working escape at all (confirmed empirically against real ffmpeg) and crashed the render on any on-screen text containing `%`. Font: 60px white, black shadow, centered at 73% of frame height. Up to 5 segments per beat.

---

## Asset sourcing

### Stock footage (`PexelsVideoSource`)

- Portraits at ≤ FHD (1920 px) preferred; falls back gracefully to landscape or 4K
- Cached by `(source="pexels", source_ref=video_id)` in `assets` table
- `safe_to_publish=True` (Pexels license is permissive)

### Player photos (`WikipediaImageSource`)

- Triggered when a person name (`engine/names.py::person_names()` — clubs and tournaments are excluded) is found in `visual_direction`
- Flow: opensearch → page summary REST → image download + `extmetadata` license fetch (filename `urllib.parse.unquote()`-decoded before the MediaWiki `titles=` lookup — an encoded filename otherwise matches no page and silently defaults to `license="unknown"`)
- Rate-limit handling: 0.5 s between players, 2 s retry on 429
- License fields stored: `license`, `license_url`, `attribution`, `safe_to_publish`
- `safe_to_publish` is `True` only for CC0/CC-BY/public domain — most player headshots are CC-BY-SA (attribution required at publish)

### Generated fallback (HuggingFace)

- `HuggingFaceVideoSource` (LTX-Video) and `HuggingFaceImageSource` (FLUX.1-schnell) — last resorts in the chain, before a black frame
- Both cache by prompt fingerprint; cost (`asset_hf_video`/`asset_hf_image` StageEvents) is charged only on an actual generation call, never a cache hit
- Always `safe_to_publish=True`

### TTS audio

`tts_provider` config defaults to `"edge"`. Valid values are `edge`, `kokoro`, and `silent`; anything else logs a warning and falls back to `SilentProvider` (shared 1 s placeholder file for every beat).

`EdgeTTSProvider` (activated by `TTS_PROVIDER=edge`):
- `_normalize_for_tts()`: contraction restoration → acronym expansion → diacritic stripping
- `synthesize(text, rate="+0%")`: cache key = `sha256(voice + rate + text)`; atomic tmp-then-replace write; one retry on a 60 s timeout
- `synth_to_budget(text, target_s)`: synthesizes at default rate, measures duration, re-synthesizes with adjusted rate if drift > 15%
- `Reel.tts_voice` (nullable) lets each reel pick one of `CURATED_EDGE_VOICES` — edge provider only

`KokoroProvider` (activated by `TTS_PROVIDER=kokoro`, requires Python < 3.13) — local neural TTS, no per-reel voice choice yet.

### Caption export

`composite_cut()` writes an `.srt` file alongside the MP4 (`engine/render/srt.py::write_srt()`) from Whisper's `.segments` (or the proportional vo_script-sentence-split fallback), shifted to the reel's absolute timeline. `Cut.subtitle_path` is `None` when there was nothing to caption (e.g. silent `voiceover_mode`). Served at `GET /api/cuts/{id}/subtitles`; also best-effort-uploaded to YouTube via `captions.insert` at publish time.

---

## Publishing

`engine/publish/` holds one `Publisher` per platform behind `engine/publish/registry.py::get_publisher()`. `YouTubePublisher` does a resumable upload (init POST + PUT) and refreshes expired tokens via a stored refresh token; `InstagramPublisher` requires a real public HTTPS `public_base_url` (Instagram's Graph API fetches the video itself); `TikTokPublisher.publish()` raises `NotImplementedError` on purpose.

Three gates run in `publish_cut`, in order, before any upload:
1. `assert_safe_to_publish()` — every bound `CutAsset`'s `Asset.safe_to_publish`
2. `assert_video_matches_pins()` — `Cut.rendered_pins_fingerprint` vs. a fresh `compute_pins_fingerprint_for_render()` (catches a re-pin followed by a render that failed before `video_path` caught up)
3. `assert_video_matches_guide()` — `Cut.rendered_guide_fingerprint` vs. a fresh `compute_guide_fingerprint(cut.guide)` (catches a guide edit followed by a failed re-render)

Both fingerprint checks treat `None` as "unknown, don't block" (pre-migration/never-rendered rows), never as a mismatch. None of the three gates run on the "finalize" branch (a cut that already has `platform_post_id` — nothing is uploaded there, so blocking it would leave a live post unrecorded with no operator way out).

`Credential.token_blob`/`refresh_token_blob` are transparently Fernet-encrypted (`api/crypto.py::Encrypted`); application code reads/writes them as plain strings. `pull_publish_metrics()` (Celery beat, every 6 h) refreshes YouTube tokens proactively (short-lived, ~1 h) and best-effort-skips a cut whose platform has no `MetricsFetcher` or whose credential pull raises.

---

## Insights & performance feedback

`engine/analytics/correlation.py::quality_engagement_correlation()` computes a Pearson `r` between each reel's latest `quality_score` (`engine/observability.py::latest_quality_scores()`) and its max per-cut `views`, refusing below `MIN_SAMPLE=5` reels or on zero variance in either series (`GET /api/insights` always shows `r` with `sample_size` together, with a permanent restriction-of-range caveat). `top_bottom_performers()` returns a top/bottom-3 table (or one combined list below `2×k`) for the operator to write `PerformanceNote`s from — a deliberate human-in-the-loop design, not automatic few-shot injection of raw past-reel content (see `docs/specs/2026-09-phase5-quality-engagement-feedback.md`). Every active note is seeded into `generate_guide`'s standard-path `prior_feedback`; `evaluator_axis_weight_multipliers` is a manual, human-set per-axis scoring lever informed by the same data — nothing in this repo fits it statistically.

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
                               default: nvidia/nemotron-3-super-120b-a12b @ integrate.api.nvidia.com/v1
  else:                     →  OllamaProvider(LLM_BASE_URL, LLM_ENRICHMENT_MODEL)
                               default: qwen3:14b @ localhost:11434/v1
```

`OllamaProvider` is fully OpenAI-compatible (`/chat/completions`), with a 360 s HTTP timeout. The `api_key` adds `Authorization: Bearer {key}`.

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
  platform: "youtube_shorts" | "instagram_reels" | "tiktok"
  target_length_s: float
  beats: list[Beat]               # min 3; first=hook, last=cta
  caption: str
  hashtags: list[str]             # 5–25, no # prefix

MasterGuide:
  title: str
  niche: str
  cuts: list[PlatformGuide]       # one per requested platform
```

`compute_guide_fingerprint(guide)` — sha256 of `json.dumps(guide, sort_keys=True)` — fingerprints a guide's content for the publish-time staleness gate; dict-key order is normalized, list order (beats, hashtags) is deliberately significant.

---

## What is not yet built

- **TikTok publishing**: `CutPlatform.tiktok` exists (render/review works); `TikTokPublisher.publish()` raises `NotImplementedError` on purpose — the Content Posting API needs a separate audited app review, unlike YouTube/Instagram's self-serve OAuth
- **Scheduling trigger**: `scheduled` cut status and `publish_cut` both handle a cut already sitting in `scheduled`; nothing currently transitions a cut *into* it (no date/time picker, no beat-driven scheduled publish)
- **Multi-image collage beyond 2 items**: a beat with 3+ resolved media items still cycles sequentially; a 2-item beat gets the side-by-side collage (shipped — see "Render pipeline detail")
- **Unpublish / re-publish flows**: a published cut has no "take down" or "publish again" action
- **Analytics beyond raw views/likes/comments and quality↔views correlation**: `cut_metric_snapshots` (Phase 7p) now accumulates history, but no chart/trend UI reads it yet; no engagement-rate normalization, no per-axis correlation (which of the 17 evaluator axes predicts engagement), no per-niche correlation, no likes/comments composite metric
- **Automatic/statistical tuning of `evaluator_axis_weight_multipliers`**: the lever exists for a human to act on the correlation data; fitting it statistically from an n≈10–20 sample is the same overfitting risk `PerformanceNote`s were designed to avoid on the prompt side
- **`PIXABAY_API_KEY`**: unused by design — Pixabay's public REST API has never documented a Music endpoint; music sourcing uses `LocalMusicSource` against an operator-populated local library instead

Everything else referenced in CLAUDE.md's Build phase status as shipped (word-level SRT caption export, YouTube/Instagram OAuth publishing, `safe_to_publish`/pins/guide staleness gates, hook-variant and thumbnail-variant generation, per-reel TTS voice and text color, music mixing with sidechain ducking, HF asset-generation cost tracking, caption attribution, post-publish metrics pull-back, quality↔engagement correlation and performance-informed feedback, the reaper-resume/fencing-token mechanism, and the Phase 7q/7r/7s refactors of `generate_guide()`/`compositor.py`/`evaluator.py`) is reflected in this document. See CLAUDE.md's "Build phase status" section for the full phase-by-phase history and `docs/roadmap.md` for sequencing rationale.
