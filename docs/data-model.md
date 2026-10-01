# Data Model

All tables are defined in `api/models.py`. Migrations: `0001_initial.py` (base schema) + `0002_improvements.py` (pinning, licensing, observability, heartbeat) + `0003_context_enrichment.py` (enriched_context column, enriching/enrich enum values) + `0004_publishing.py` (tiktok platform, Credential refresh/account-id columns) + `0005_metrics.py` (Cut engagement columns) + `0006_variants.py` (Cut thumbnail/hook-variant columns) + `0007_performance_notes.py` (`performance_notes` table) + `0008_tts_voice.py` (Reel.tts_voice column) + `0009_text_color.py` (Reel.text_color column) + `0010_black_frame_visibility.py` (Cut.black_frame_beat_indices column) + `0011_subtitle_caption_export.py` (Cut.subtitle_path column) + `0012_rendered_pins_fingerprint.py` (Cut.rendered_pins_fingerprint column) + `0013_reaper_resume_and_claim_token.py` (Job.reaper_resumes/Job.claim_token columns) + `0014_rendered_guide_fingerprint.py` (Cut.rendered_guide_fingerprint column) + `0015_cut_metric_snapshots.py` (`cut_metric_snapshots` table). Video and audio files live on disk under `ASSET_STORE_DIR` / `VIDEO_STORE_DIR`; the DB stores paths, never blobs.

---

## Tables

### `reels`

One reel = one topic/context. Owns two cuts (one per platform) and all jobs.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `context` | text | User's topic/prompt — required; always the raw input, never modified |
| `enriched_context` | text | LLM-enriched version; set only when context scores < 60; nullable |
| `niche` | varchar(255) | Optional category label |
| `voiceover_mode` | varchar(50) | `voiceover` \| `music_only` \| `silent` |
| `tts_voice` | varchar(100), nullable | Edge-tts voice name from `CURATED_EDGE_VOICES`; `None` = provider default. Ignored when `TTS_PROVIDER=kokoro`. |
| `text_color` | varchar(20), nullable | On-screen text color from `CURATED_TEXT_COLORS`; `None` = `DEFAULT_TEXT_COLOR` ("white"). Interpolated unescaped into the ffmpeg drawtext filter — validated at both the router and `_build_text_filter()`, not just curated for UX. |
| `status` | enum `ReelStatus` | See state machine |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

**ReelStatus:** `draft` → `enriching` → `generating` → `guide_ready` → `failed`

---

### `cuts`

One row per platform per reel. All generated content for a platform lives here.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `reel_id` | FK → reels | |
| `platform` | enum `CutPlatform` | `youtube_shorts` \| `instagram_reels` \| `tiktok` |
| `target_length_s` | float | Requested duration (30/45/60/75/90 s) |
| `guide` | JSON | Full `PlatformGuide` dict — see guide schema below |
| `caption` | text | Post caption (DB-stored value; the published caption additionally gets an attribution suffix appended at publish time only, via `build_published_caption()` — this column is never mutated with it) |
| `hashtags` | JSON array | List of strings, no `#` prefix |
| `video_path` | varchar(500) | Absolute path to rendered MP4 on disk |
| `thumbnail_path` | varchar(500) | Absolute path to thumbnail JPEG — whichever of `thumbnail_candidates` is currently chosen, `[0]` by default |
| `thumbnail_candidates` | JSON, nullable | All 4 candidate frames `render_cut` samples across the reel (the original ~0.5s-in frame plus 3 more). Operator picks one via `POST /cuts/{id}/thumbnail`; a re-render replaces the list wholesale, discarding any prior pick. |
| `hook_variants` | JSON, nullable | Up to 3 alternate opening-line strings for the hook beat, generated once per accepted guide (`generate_hook_variants()`, best-effort — never fails the job). `None` if generation failed or the paid-call budget was already spent. Operator swaps one in via `POST /cuts/{id}/hook-variant` (gated to `in_review`, beat 0 only). |
| `black_frame_beat_indices` | JSON, nullable | 0-indexed beats where the asset-sourcer fallback chain (Wikipedia → Pexels → HF Video → HF Image) found nothing and the compositor rendered a black frame for that beat's full duration. `None` when every beat resolved real media. Written by `render_cut`, replaced wholesale on re-render. |
| `rendered_pins_fingerprint` | varchar(64), nullable | Fingerprint of the `CutAsset` pins that built the currently-stored `video_path`, snapshotted at the moment a render succeeds via `engine/render/asset_sourcer.py::compute_pins_fingerprint_for_render()` — a real sha256 hash for a normal render, or the fixed `EMPTY_PINS_FINGERPRINT` sentinel (never the raw `compute_pins_fingerprint()`'s `None`) for a successful render that bound zero real assets (every beat black-framed), so a completed render is always distinguishable from "never rendered." `None` means "no completed render has ever written this column" — not yet rendered, or rendered before this column existed (a legacy row — treated as "unknown, don't block" by `engine/publish/gate.py::assert_video_matches_pins()`, not as a mismatch). Written by `render_cut`, replaced wholesale on re-render, same policy as `black_frame_beat_indices`/`thumbnail_candidates`/`video_path`. Compared against a freshly-computed current fingerprint at publish time to catch a re-render that re-pinned an asset and then failed before `video_path` caught up — see CLAUDE.md's Key conventions entry and `docs/specs/2026-09-video-pins-staleness-gate-system-design.md`. |
| `rendered_guide_fingerprint` | varchar(64), nullable | The guide-content sibling of `rendered_pins_fingerprint` above, same `None`-means-not-yet-rendered semantics — a sha256 fingerprint (`engine/generation/guide_schema.py::compute_guide_fingerprint()`) of the `guide` dict that built the currently-stored `video_path`, snapshotted by `render_cut` alongside `rendered_pins_fingerprint`. Compared against a fresh fingerprint of the current `guide` at publish time by `engine/publish/gate.py::assert_video_matches_guide()` to catch a guide edit (`PATCH /cuts/{id}`, or a hook-variant swap) followed by a re-render that failed before `video_path` caught up — see CLAUDE.md's Key conventions entry and `docs/specs/2026-09-stale-video-on-failed-rerender-system-design.md`. |
| `subtitle_path` | varchar(500), nullable | Absolute path to the SRT caption file `composite_cut()` writes alongside the MP4 (Whisper `.segments`, or the proportional vo_script-sentence-split fallback when Whisper isn't installed). `None` when the render produced nothing to caption (e.g. silent voiceover_mode) or the cut predates this feature. Written by `render_cut`, replaced wholesale on re-render — see `engine/render/srt.py`. |
| `duration_s` | float | Actual rendered duration in seconds |
| `status` | enum `CutStatus` | See state machine |
| `published_at` | timestamptz | Set when publish completes |
| `platform_post_id` | varchar(255) | YouTube video ID or IG media ID |
| `views` | integer | Latest pull from `pull_publish_metrics()`; `null` until first pull |
| `likes` | integer | Latest pull from `pull_publish_metrics()`; `null` until first pull |
| `comments` | integer | Latest pull from `pull_publish_metrics()`; `null` until first pull |
| `metrics_updated_at` | timestamptz | Timestamp of the last successful metrics pull; `null` until first pull |

**CutStatus:** `draft` → `rendering` → `in_review` → `approved` → `publishing` → `published` (also `scheduled`, `failed`)

---

### `cut_metric_snapshots`

Append-only metrics history, alongside (never replacing) `cuts.views`/`likes`/`comments`/
`metrics_updated_at` above, which remain the single "latest known" source every existing reader
uses (reel list, insights page, correlation, the cut card). One row is written per
`pull_publish_metrics()` reading that actually reached a fetch (no row when there's no credential,
no fetcher for the platform, or the fetch itself raised). Closes the "`pull_publish_metrics()`
overwrites rather than accumulates a time series" Open Issues item — no chart/trend UI reads this
table yet; it exists to start accumulating history now for a future analytics feature to consume.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `cut_id` | FK → cuts | |
| `views` | integer, nullable | The RAW value this specific pull returned — `null` when this pull didn't return this metric (e.g. Instagram metric-name drift), never forward-filled from the cut's prior known value. |
| `likes` | integer, nullable | Same raw-value semantics as `views`. |
| `comments` | integer, nullable | Same raw-value semantics as `views`. |
| `recorded_at` | timestamptz | Set to the exact same timestamp as this pull's `cuts.metrics_updated_at` write (captured once, reused for both) — not merely close in time. |

No ORM relationship is declared on `Cut` for this table (modeled on `stage_events` below, which is
also queried directly rather than through a collection). See
`docs/specs/2026-09-metrics-history-system-design.md`.

---

### `assets`

Cached media files. Deduplicated by `(source, source_ref)` — re-renders and re-generations reuse without re-downloading.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `type` | varchar(50) | `footage` \| `photo` |
| `source` | varchar(100) | `pexels` \| `wikipedia` \| `huggingface` \| `huggingface_video` |
| `source_ref` | varchar(255) | Pexels video ID or Wikipedia page ID |
| `local_path` | varchar(500) | Absolute path to downloaded file |
| `license` | varchar(255) | e.g. `pexels_free`, `CC BY-SA 4.0` |
| `license_url` | varchar(500) | URL to license text (from Wikipedia extmetadata) |
| `attribution` | text | Credit string for photo (artist/Wikimedia Commons) |
| `safe_to_publish` | boolean | `true` for CC0/CC-BY/Pexels; `false` for CC-BY-SA, non-free |
| `created_at` | timestamptz | |

Cache lookup key: `(source, source_ref)`. Always call `resolve_or_reuse()` or `resolve_beat_assets()` rather than hitting the external API directly.

**Publishing gate**: check `safe_to_publish == True` before allowing a cut to publish. Wikipedia player headshots are frequently CC-BY-SA (attribution required); `attribution` field provides the credit text to append to the caption.

---

### `cut_assets`

Per-beat asset binding ledger. Records which asset was pinned for which beat, and at what timeline position. **Unique constraint: `(cut_id, beat_index, order_in_beat)`**.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `cut_id` | FK → cuts | |
| `asset_id` | FK → assets | |
| `role` | varchar(100) | `footage` |
| `beat_index` | integer | Beat position in the guide (`beat.index`) |
| `order_in_beat` | integer | For multi-asset beats (multiple players); 0-based |
| `resolved_from` | varchar(16) | `sha256(visual_direction)[:16]` — fingerprint of the direction that produced this binding |
| `start_s` | float | Beat start offset in the final video (TTS-accurate, updated after render) |
| `end_s` | float | Beat end offset (TTS-accurate) |

**Re-render behaviour**: `resolve_or_reuse()` compares the current `visual_direction` fingerprint against `resolved_from`. If they match, the pinned asset is reused without any API call. If they differ (operator edited the visual direction), the old rows for that beat are deleted and re-resolved. This means unchanged beats never trigger Pexels/Wikipedia calls on re-render.

---

### `jobs`

Every async operation is a job row. API creates the row and enqueues the task with the job ID. Worker updates `status`, `progress`, and `heartbeat_at`. Browser polls a fragment endpoint that reads this table.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `type` | enum `JobType` | `enrich` \| `generate` \| `render` \| `publish` |
| `reel_id` | FK → reels | Set for enrich/generate jobs |
| `cut_id` | FK → cuts | Set for render/publish jobs |
| `status` | enum `JobStatus` | `pending` → `running` → `done` \| `failed` |
| `progress` | integer | 0–100; updated at key milestones |
| `error` | text | Exception message (≤ 2000 chars, NUL/surrogates stripped, `[parameters: …]` redacted); `null` on success |
| `attempts` | integer, default `0` | Incremented once per run, after `prepare` succeeds (so a missing-row or budget failure does not count), including each retry delivery |
| `reaper_resumes` | integer, default `0`, `server_default="0"` | How many times `reap_stuck_jobs` has resumed this row in place (`pending→running`, re-enqueued), checked against a per-job-type budget in `worker/tasks/maintenance.py::_RESUMABLE_TASKS` (`enrich`/`render`: 2, `generate`: 1, `publish`: never resumed). Migration `0013`. |
| `claim_token` | integer, default `0`, `server_default="0"` | Fencing counter, bumped via a SQL-side increment on every `pending→running` claim and captured once per run (see CLAUDE.md's Key Conventions on why a plain re-read after a rollback would be unsafe). Lets a superseded (resumed-while-still-alive) run's writes be told apart from a fresher claim's even while `status` alone still reads `running`. Migration `0013`. |
| `started_at` | timestamptz | Set after `prepare`, when the body is about to run (the claim moves the job to `running` a moment earlier) |
| `heartbeat_at` | timestamptz | Refreshed every 30 s by a background thread in `job_task` (until the task's `max_runtime_s`) and at every `heartbeat()` call; used by the stuck-job reaper |
| `meta` | JSON | Enrich job: `{"generation_path": "auto\|structured\|standard", "context_score": N, "context_issues": [...], "enriched": bool}`. Generate job: `{"generation_path": ..., "context_score": N, "path": "structured\|standard", "stub_count": N, "quality_score": N, "structured_score": N, "structured_fallback": bool, "performance_note_ids": [N, ...]}` — `generate_guide` strips `structured_fallback`/`structured_score`/`path` at function entry so a prior killed-and-resumed attempt's leftovers can't leak into a clean run's final meta. |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

**Stuck-job detection**: `reap_stuck_jobs` (Celery beat, 60 s interval) handles three stale cases — `status=running` with `heartbeat_at` older than 5 min (resumes in place for a resumable type under budget, else fails it and rolls back the owning reel/cut); `status=pending` with `updated_at` older than 240 min (never picked up); and `status=done` with no error whose owner is still mid-flight for 15 min (the `after_commit_failed` fail-stamp itself never landed). See `docs/architecture.md`'s Reliability section and CLAUDE.md's Key Conventions for the full fencing-token mechanism this depends on.

**Idempotency**: only a `pending` job runs (`job_task`'s atomic `UPDATE … WHERE status='pending'` claim, which also bumps `claim_token`). `done`/`running` = redelivery no-op (a live sibling is already working, or the reaper will eventually reap a dead one). `failed` is terminal: an operator retry creates a new Job, so a late redelivery of a reaped job never runs.

---

### `stage_events`

Instrumentation for each slow pipeline stage. One row per stage invocation.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `reel_id` | FK → reels, not nullable | |
| `cut_id` | FK → cuts, nullable | Null for reel-level (enrich/generate) stages |
| `stage` | varchar(50) | `context_enrich` \| `enrich` \| `enrich_conflict` \| `visuals` \| `generate` \| `judge` \| `caption_hashtags` \| `composite` \| `asset_hf_video` \| `asset_hf_image` \| `publish` \| `captions_upload` \| `instagram_metrics` |
| `provider` | varchar(100) | `ollama` \| `nvidia` \| `edge` \| `huggingface` |
| `model_name` | varchar(255) | LLM model identifier |
| `latency_ms` | integer | Wall-clock time for the stage |
| `tokens_in` | integer | Input tokens — populated for NVIDIA LLM calls via `OllamaProvider`'s usage capture |
| `tokens_out` | integer | Output tokens — same as above |
| `cost_usd` | float | Computed cost — populated for NVIDIA LLM calls (`engine/generation/pricing.py`) and HF asset generation (`engine/render/pricing.py`); both `0.0` until the operator sets a real per-unit rate in `Settings`. `asset_hf_*` stages only charge `cost_usd` on an actual generation call, never a cache hit. |
| `attempt` | integer | Retry number (1, 2, 3) for multi-attempt stages |
| `score` | integer | Quality score (for eval stages) |
| `ok` | boolean | `true` = completed without exception (also explicitly set `false` for a "never raises, but still tell the truth" best-effort step — e.g. `captions_upload`, `instagram_metrics` metric-name drift) |
| `detail` | JSON | Stage-specific payload: error text, failure reasons, `missing_metrics`, raw lengths, etc. |
| `created_at` | timestamptz | |

Written by `record_stage()` context manager in `engine/observability.py`. A row is written even on failure (`ok=false`, `detail.error=repr(exc)`), and `record_stage()` always re-raises on an exception inside its `with` block — a step that must never fail its caller (captions upload, Instagram metrics) sets `ev.ok = False` explicitly from *inside* the block and returns normally instead of relying on the context manager to swallow anything.

The `/api/reels/{id}` pipeline panel's headline `total_cost`/`total_latency_ms` sums exclude `instagram_metrics` (it fires every 6 h indefinitely for a published cut, unlike every bounded generation/render stage) but the per-stage `stage_summary` table stays unfiltered.

---

### `credentials`

OAuth tokens for publishing. `token_blob`/`refresh_token_blob` are encrypted at rest and transparently decrypted on read — application code never calls `crypto.open_()`/`seal()` directly, just assigns/reads the attribute.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `provider` | varchar(100) | `youtube` \| `instagram` (not the `CutPlatform` value — see `credential_provider_for_platform()`) |
| `account_label` | varchar(255) | Human-readable account name (e.g. `"Instagram business account {id}"`) |
| `token_blob` | Encrypted text | Fernet-encrypted access token; transparent via the `Encrypted` TypeDecorator |
| `refresh_token_blob` | Encrypted text, nullable | Fernet-encrypted OAuth refresh token (YouTube only — Instagram's long-lived token has no refresh token) |
| `provider_account_id` | varchar(255), nullable | A provider-specific ID discovered during OAuth that isn't itself a scope — e.g. the Instagram Business Account ID behind a connected Facebook Page |
| `scopes` | JSON | OAuth scopes granted, split from the token response's space-separated `scope` string |
| `expires_at` | timestamptz, nullable | |

Set `CREDENTIALS_KEY` in `.env` (a 32-byte URL-safe base64 Fernet key). Rotating the key invalidates stored tokens — just re-auth. If the key is not set, values are stored as plaintext with a logged warning — safe for dev, not for production.

---

### `performance_notes`

Operator-written, plain-English notes synthesizing past reel performance (e.g. "Hooks phrased as a direct question outperform statement hooks — lean into that"), added from the `/api/insights` page after reviewing the top/bottom performer report. A standalone table — no FK to `reels`/`cuts`; a note is a general observation, not tied to one reel.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `text` | text | Operator-written note; required |
| `active` | boolean | Default `true`. Only active notes are seeded into `generate_guide`'s `prior_feedback` (standard LLM path only) — see `docs/evaluation.md`'s "Performance-informed feedback" section |
| `created_at` | timestamptz | |

Deliberately **not** automated few-shot injection of raw past-reel content — see `docs/specs/2026-09-phase5-quality-engagement-feedback.md` §3.1. CRUD is hard-delete (no soft-delete/undo — these are cheap, operator-owned free text, unlike a `Job`/`Cut` state machine): `POST /api/insights/notes` (create, `active=true`), `POST /api/insights/notes/{id}/toggle` (flip `active`), `DELETE /api/insights/notes/{id}`.

`job.meta["performance_note_ids"]` on a `generate` job records which notes were active for that run — written by the same shared `job.meta` line as `quality_score` (reached by both the structured and standard generation paths).

---

## Entity relationships

```
reels (1) ──── (N) cuts
reels (1) ──── (N) jobs
reels (1) ──── (N) stage_events
cuts  (1) ──── (N) cut_assets ──── (N) assets
cuts  (1) ──── (N) jobs                  [render/publish jobs]
cuts  (1) ──── (N) stage_events
cuts  (1) ──── (N) cut_metric_snapshots

credentials         — standalone, keyed by `provider`, no FK to reels/cuts
performance_notes   — standalone, no FK — a general observation, not tied to one reel
```

No ORM `relationship`/`back_populates` is declared for `cut_metric_snapshots` or `stage_events` — both are queried directly rather than through a collection, matching `StageEvent`'s own pre-existing pattern.

---

## State machines

Defined in `api/state.py`. Call `transition(obj, new_status, MAP)` — raises `ValueError` on invalid moves. Never set `.status` directly.

### Reel

```
          ┌──────────────────────────────────────────┐
          │                                          ▼
draft ──► enriching ──► generating ──► guide_ready
               │              │
               └──► failed ◄──┘
                       │
                       └──► draft
```

| From | To | Trigger |
|---|---|---|
| `draft` | `enriching` | `POST /api/reels` |
| `enriching` | `generating` | `enrich_context` success |
| `enriching` | `failed` | `enrich_context` hard exception |
| `generating` | `guide_ready` | `generate_guide` success |
| `generating` | `failed` | `generate_guide` exception |
| `failed` | `draft` | Manual retry / reaper rollback |

`REEL_TRANSITIONS` (`api/state.py`) also permits `draft → generating` and `guide_ready → failed` directly. Neither edge is exercised by any current code path (no router or task transitions a reel straight from `draft` to `generating`, and `guide_ready` is not an owned in-flight state in `JOB_IN_FLIGHT` for any job to roll back) — they appear to be defined defensively rather than reachable today; verify against `api/state.py` before relying on either.

### Cut

```
                             ┌──────────────┐
                             ▼              │
draft ──► rendering ──► in_review ──► rendering  (re-render)
               │              │
               │              └──► approved ──► publishing ──► published
               │                        │             │
               │                        └──► scheduled ──► publishing
               └──► failed ──┬──► draft (render retry)
                              └──► approved (publish retry)
```

| From | To | Trigger |
|---|---|---|
| `draft` | `rendering` | `POST /api/cuts/{id}/render` |
| `rendering` | `in_review` | `render_cut` success |
| `rendering` | `failed` | `render_cut` exception |
| `in_review` | `rendering` | Re-render button |
| `in_review` | `approved` | Approve → `POST /api/cuts/{id}/approve` |
| `approved` | `publishing` | `POST /api/cuts/{id}/publish` |
| `approved` | `scheduled` | No trigger exists yet — the transition is defined and `publish_cut` handles a cut already in `scheduled`, but nothing currently moves a cut *into* it (no date/time picker, no beat-driven scheduler) |
| `scheduled` | `publishing` | Same `POST /api/cuts/{id}/publish` endpoint handles a cut already in `scheduled` |
| `publishing` | `published` | `publish_cut` success (upload live, `platform_post_id` committed) |
| `publishing` | `failed` | `publish_cut` exception (safety/staleness gate, upload error) |
| `failed` | `draft` | Render retry — `trigger_render()` auto-resets `failed` → `draft` before re-rendering, or reaper rollback after a failed render job |
| `failed` | `approved` | Publish retry — `trigger_publish()` auto-resets `failed` → `approved` (no re-render needed: bad credentials, network error, a safety/staleness gate), or reaper rollback after a failed publish job |

`"failed"` is deliberately ambiguous between a failed render and a failed publish — see `api/state.py::CUT_TRANSITIONS`'s own comment. Which retry target applies is decided by which endpoint the operator hits, not tracked on the Cut itself. `latest_failed_job_for_cut()` (`api/routers/cuts.py`) surfaces the real `job.error` for whichever failure actually happened, regardless of this ambiguity.

---

## Guide JSON schema

`cuts.guide` stores a serialized `PlatformGuide`. Always deserialize with `PlatformGuide(**cut.guide)` before use.

```json
{
  "platform": "youtube_shorts",
  "target_length_s": 45.0,
  "beats": [
    {
      "index": 0,
      "type": "hook",
      "duration_s": 2.5,
      "visual_direction": "person staring at empty wallet, close-up hands",
      "on_screen_text": ["BROKE at 25"],
      "vo_script": "I was completely broke at 25.",
      "music_cue": "tense minimal",
      "transition": "cut"
    },
    {
      "index": 1,
      "type": "body",
      "duration_s": 8.0,
      "visual_direction": "Lionel Messi through-ball key pass threading defense",
      "on_screen_text": ["Habit #1", "Track every dollar", "Every. Single. One."],
      "vo_script": "The first habit that changed everything was tracking every single dollar.",
      "music_cue": null,
      "transition": "cut"
    },
    {
      "index": 2,
      "type": "cta",
      "duration_s": 3.0,
      "visual_direction": "person smiling at camera, bright background",
      "on_screen_text": ["Follow for more tips"],
      "vo_script": "Follow for more money tips like this.",
      "music_cue": "uplifting",
      "transition": "fade"
    }
  ],
  "caption": "From broke to $10k saved in one year.",
  "hashtags": ["personalfinance", "moneytips", "savingmoney", "financialfreedom", "budgeting"]
}
```

`cuts.hashtags` is a separate top-level JSON array (same data as `guide.hashtags`) for easy access.
