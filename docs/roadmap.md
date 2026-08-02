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
| 4 | 🔲 Next | Publishing |
| 5 | 🔲 Planned | Analytics and polish |

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

## Phase 4 — Publishing (next)

### 4a. YouTube Shorts upload

**Prerequisites:**
- `CREDENTIALS_KEY` set and a `Credential` row for the `youtube` provider
- `safe_to_publish == True` for all assets in the cut (gate in publish worker)

**Work:**
1. OAuth 2.0 flow: `/auth/youtube` redirect → callback → `Credential` row (token encrypted)
2. `worker/tasks/publish.py`: `publish_cut(job_id)` — upload `.mp4` via YouTube Data API v3 (`videos.insert`), set title/description/hashtags, return `platform_post_id`
3. Attribution block: assemble Wikipedia attribution strings from `cut_assets → assets.attribution` and append to caption before upload
4. State transitions: `approved → publishing → published` (or `failed`)
5. `published_at` and `platform_post_id` set on the cut row

**Migration needed:** none — all columns exist (`platform_post_id`, `published_at`, `Credential`)

### 4b. Instagram Reels upload

Same pattern as YouTube. Instagram Graph API requires a two-step flow (create container → publish). Separate `Credential` row for `instagram` provider.

### 4c. Publish gate UI

- Approve button should be blocked (or warn) when any beat's asset has `safe_to_publish == False`
- Show attribution preview in the cut card before approving

---

## Phase 5 — Analytics and polish

### 5a. Cost tracking

`StageEvent.tokens_in`, `tokens_out`, `cost_usd` are already columns; pricing lookup is not implemented.

**Work:**
- Add `_price(model, tokens_in, tokens_out) → float` lookup table (NVIDIA NIM pricing)
- Populate `cost_usd` in `record_stage()` after yield
- Add per-reel cost line to the guide detail page: `Σ stage_events.cost_usd WHERE reel_id=N`
- Add per-stage latency breakdown (which stage dominates?)

### 5b. Post-publish metrics

- YouTube/IG API pull-back: views, watch time, shares at 24 h / 7 d
- `metrics` table: `(cut_id, platform, fetched_at, views, watch_time_s, shares, ctr)`
- Celery beat task: fetch metrics for all `published` cuts older than 24 h
- Surface on guide page: spark line of view count over time

### 5c. Music mixing

`music_cue` field is generated by the LLM but no audio is fetched or mixed.

**Intended approach:**
- Source royalty-free tracks by mood keyword (Pixabay music API or local library)
- FFmpeg `amix` filter to layer music under VO
- `agate` sidechain compressor: duck music -18 dB under VO, recover between beats
- `Asset(type="music", source="pixabay")` row for caching

### 5d. Word-level caption export

Whisper is wired in for on-screen text timing, but a separate SRT/VTT caption file for the platform uploader is not generated.

**Work:**
- After Whisper transcription in the compositor, write per-beat segments to an SRT file alongside the MP4
- Pass the SRT to YouTube Data API as a caption track on upload

### 5e. ~~Insight enrichment for standard LLM path~~ ✅ Done (Phase 3.6)

`_enrich_standard_path_guide()` runs after `clean_guide()` in the standard path — converts beats to `BeatStub`-like objects, runs `_enrich_with_insight()`, writes enriched VO + recalculated duration + re-derived on_screen_text back.

---

## Open issues

| Issue | Severity | Notes |
|---|---|---|
| No multi-image collage in one frame | Low | Currently cycles sequentially; side-by-side layout not implemented |
| MoviePy video readers leak until worker recycle | Low | `_build_media_sub_clip` opens `VideoFileClip`s that only `worker_max_tasks_per_child=10` reclaims; marked with a `ponytail:` comment |
| `asset_sourcer` degrades silently to black frames | Medium | Every sourcer swallows its own exceptions and returns `None`, so a Pexels/Wikipedia outage produces a black-frame reel that reports success — and never reaches the retry branch |
| `record_stage()` commits the caller's session | Low | Benign today (all call sites sit on a commit boundary) and documented in `observability.py`, but it will bite whoever wraps a half-applied mutation |
| `_escape_drawtext` escapes only `\ : % '` | Low | A newline or exotic character in `on_screen_text` could break the FFmpeg filter chain; not observed in practice |
| Wikipedia licence lookup uses a percent-encoded filename | Low | `_fetch_license()` passes the raw URL segment, so accented/spaced filenames return "unknown" and default to `safe_to_publish=False`. Matters at Phase 4 publish time |
| Cut page does not poll while rendering | Low | `cut_card.html` shows "refresh to update" instead of an auto-refreshing fragment |
| LLM judge 60% weight can swing combined score | Low | Log per-attempt rule/judge split from `StageEvent`; tune once data accumulates |
| Whisper `base` model is slow on CPU | Low | Switch to `faster-whisper` with `base` model for 3-4× speedup on same hardware |
| No OAuth refresh token rotation | Medium | Credential encrypted but no auto-refresh; expired tokens silently fail at publish |
| Standard LLM path caption/hashtags not content-aware | Low | Generated from niche only, not actual beat content; structured path uses `_generate_caption_hashtags()` from actual VO — standard path could do the same |
| `PIXABAY_API_KEY` config field is unused | Low | Added to config in anticipation of Pixabay music/video integration (Phase 5c); no provider wired yet |

---

## Dependency notes

| Dep | Status | Notes |
|---|---|---|
| `edge-tts` | Required | Not in pyproject.toml extras; `pip install edge-tts` separately |
| `openai-whisper` | Optional | `pip install -e ".[captions]"`; without it, proportional text timing is used |
| `faster-whisper` | Not installed | Drop-in replacement for `openai-whisper`; same `transcribe_audio()` interface |
| `kokoro` | Optional | Requires Python < 3.13; not compatible with Python 3.14 |
| `cryptography` | Required for publish | Fernet; no-op import check in `api/crypto.py` if not installed |
