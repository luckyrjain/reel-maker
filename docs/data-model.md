# Data Model

All tables are defined in `api/models.py`. Migrations: `0001_initial.py` (base schema) + `0002_improvements.py` (pinning, licensing, observability, heartbeat) + `0003_context_enrichment.py` (enriched_context column, enriching/enrich enum values). Video and audio files live on disk under `ASSET_STORE_DIR` / `VIDEO_STORE_DIR`; the DB stores paths, never blobs.

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
| `platform` | enum `CutPlatform` | `youtube_shorts` \| `instagram_reels` |
| `target_length_s` | float | Requested duration (30/45/60/75/90 s) |
| `guide` | JSON | Full `PlatformGuide` dict — see guide schema below |
| `caption` | text | Post caption |
| `hashtags` | JSON array | List of strings, no `#` prefix |
| `video_path` | varchar(500) | Absolute path to rendered MP4 on disk |
| `thumbnail_path` | varchar(500) | Absolute path to thumbnail JPEG |
| `duration_s` | float | Actual rendered duration in seconds |
| `status` | enum `CutStatus` | See state machine |
| `published_at` | timestamptz | Set when publish completes |
| `platform_post_id` | varchar(255) | YouTube video ID or IG media ID |

**CutStatus:** `draft` → `rendering` → `in_review` → `approved` → `publishing` → `published` (also `scheduled`, `failed`)

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
| `reel_id` | FK → reels | Set for generate jobs |
| `cut_id` | FK → cuts | Set for render/publish jobs |
| `status` | enum `JobStatus` | `pending` → `running` → `done` \| `failed` |
| `progress` | integer | 0–100; updated at key milestones |
| `error` | text | Exception message (≤ 2000 chars); `null` on success |
| `attempts` | integer | Incremented each time the worker picks up the task |
| `started_at` | timestamptz | Set when task transitions to `running` |
| `heartbeat_at` | timestamptz | Updated at every `_heartbeat()` call; used by stuck-job reaper |
| `meta` | JSON | Enrich job: `{"generation_path": "auto\|structured\|standard", "context_score": N, "context_issues": [...], "enriched": bool}`. Generate job: `{"generation_path": ..., "context_score": N, "path": "structured\|standard", "stub_count": N, "quality_score": N, "structured_score": N, "structured_fallback": bool}` |
| `created_at` | timestamptz | |
| `updated_at` | timestamptz | |

**Stuck-job detection**: if `status=running` and `heartbeat_at < now() - 5 min`, the `reap_stuck_jobs` task (Celery beat, 60 s interval) marks it `failed` and rolls back the owning reel/cut.

**Idempotency**: tasks check `if job.status in (done, running): return` at entry. `done` = redelivery no-op. `running` = live sibling already working (reaper handles dead ones).

---

### `stage_events`

Instrumentation for each slow pipeline stage. One row per stage invocation.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `reel_id` | FK → reels | |
| `cut_id` | FK → cuts | Null for generation stages |
| `stage` | varchar(50) | `enrich` \| `generate` \| `judge` \| `composite` |
| `provider` | varchar(100) | `ollama` \| `nvidia` \| `edge` |
| `model_name` | varchar(255) | LLM model identifier |
| `latency_ms` | integer | Wall-clock time for the stage |
| `tokens_in` | integer | Input tokens (not yet populated) |
| `tokens_out` | integer | Output tokens (not yet populated) |
| `cost_usd` | float | Computed cost (not yet populated) |
| `attempt` | integer | Retry number (1, 2, 3) for multi-attempt stages |
| `score` | integer | Quality score (for eval stages) |
| `ok` | boolean | `true` = completed without exception |
| `detail` | JSON | Stage-specific payload: error text, failure reasons, raw lengths, etc. |
| `created_at` | timestamptz | |

Written by `record_stage()` context manager in `engine/observability.py`. A row is written even on failure (`ok=false`, `detail.error=repr(exc)`).

---

### `credentials`

OAuth tokens for publishing (Phase 4+). `token_blob` is encrypted at rest.

| Column | Type | Notes |
|---|---|---|
| `id` | integer PK | |
| `provider` | varchar(100) | `youtube` \| `instagram` |
| `account_label` | varchar(255) | Human-readable account name |
| `token_blob` | Encrypted text | Fernet-encrypted OAuth token JSON; transparent via TypeDecorator |
| `scopes` | JSON | OAuth scopes granted |
| `expires_at` | timestamptz | |

Set `CREDENTIALS_KEY` in `.env` (a 32-byte URL-safe base64 Fernet key). Rotating the key invalidates stored tokens — just re-auth. If the key is not set, values are stored as plaintext with a logged warning.

---

## Entity relationships

```
reels (1) ──── (N) cuts
reels (1) ──── (N) jobs
reels (1) ──── (N) stage_events
cuts  (1) ──── (N) cut_assets ──── (N) assets
cuts  (1) ──── (N) jobs        [render jobs]
cuts  (1) ──── (N) stage_events
```

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

### Cut

```
                             ┌──────────────┐
                             ▼              │
draft ──► rendering ──► in_review ──► rendering  (re-render)
               │              │
               │              └──► approved ──► publishing ──► published
               │                        │
               │                        └──► scheduled ──► publishing
               └──► failed ──► draft ──► rendering
```

| From | To | Trigger |
|---|---|---|
| `draft` | `rendering` | `POST /api/cuts/{id}/render` |
| `rendering` | `in_review` | `render_cut` success |
| `rendering` | `failed` | `render_cut` exception |
| `in_review` | `rendering` | Re-render button |
| `in_review` | `approved` | Approve → `POST /api/cuts/{id}/approve` |
| `failed` | `draft` | Render retry (via `trigger_render`) / reaper rollback |
| `approved` | `publishing` | Phase 4 publish action |
| `approved` | `scheduled` | Phase 4 schedule action |
| `scheduled` | `publishing` | Phase 4 scheduler worker |
| `publishing` | `published` | Phase 4 upload success |
| `publishing` | `failed` | Phase 4 upload error |

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
