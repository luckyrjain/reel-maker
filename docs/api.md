# API Reference

All endpoints are prefixed with `/api` (5 routers: `reels`, `jobs`, `cuts`, `credentials`, `insights` — mounted in `api/main.py`). The browser receives HTML fragments (Jinja2 templates via HTMX) for every endpoint except `GET /api/jobs/{job_id}`, which is the one JSON endpoint in the app. `GET /` (not prefixed) serves the context-entry form.

---

## Reels

### `POST /api/reels`

Create a reel (+ one `Cut` per selected platform) and enqueue **context enrichment** (which in turn enqueues guide generation). Accepts `multipart/form-data`.

**Form fields:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `context` | string | yes | — | Topic/prompt for the reel |
| `niche` | string | no | `null` | Content category |
| `voiceover_mode` | string | no | `voiceover` | `voiceover` \| `music_only` \| `silent` |
| `target_length_s` | float | no | `45.0` | Target video length in seconds (30/45/60/75/90) |
| `generation_path` | string | no | `auto` | `auto` \| `structured` \| `standard` — see guide generation paths in `docs/architecture.md` |
| `platforms` | list of strings (repeated form field) | no | `["youtube_shorts", "instagram_reels"]` | One `Cut` is created per valid value in `CutPlatform` (`youtube_shorts`, `instagram_reels`, `tiktok`). Unrecognized values are silently dropped rather than causing a 422; if nothing valid remains, the default two-platform set is used. |
| `tts_voice` | string | no | `null` | Must be one of `engine/render/tts.py::CURATED_EDGE_VOICES`; any other value (including a stale/typo'd one) is silently dropped to `null` (provider default) rather than a 422 |
| `text_color` | string | no | `null` | Must be one of `engine/render/compositor.py::CURATED_TEXT_COLORS`; same silent-drop policy as `tts_voice` — this one is also a security-relevant validation, not just UX (see `docs/architecture.md`) |

**Response:** `text/html` — `fragments/pipeline_status.html` fragment polling `GET /api/reels/{reel_id}/active-job-fragment` every 2 s.

**Errors:**
- `503` — the job could not be queued (broker unreachable); the reel and job are rolled back to `failed` so it can be retried at once

---

### `POST /api/reels/estimate`

Pre-generation cost/time estimate for the create-reel form, sourced from real `StageEvent` history on the resolved generation path (not a guessed token count). Accepts `multipart/form-data`. Called via htmx as the operator fills in the form — not part of the create flow itself.

**Form fields:**

| Field | Type | Required | Default |
|---|---|---|---|
| `context` | string | no | `""` |
| `generation_path` | string | no | `auto` |

**Response:** `text/html` — `fragments/cost_estimate.html`. Reports "no history yet" rather than fabricating a number when there's no matching history.

---

### `GET /api/reels`

Paginated reel list.

**Query params:** `page` (default `1`, clamped to ≥ 1; page size is 50, `REEL_LIST_PAGE_SIZE`)

**Response:** `text/html` — `reels_list.html`. Status badges, per-cut platform badges, and two computed columns per reel: **Quality** (the latest job's `quality_score`) and **Views** (max across the reel's cuts).

---

### `GET /api/reels/{reel_id}/active-job-fragment`

HTMX polling endpoint. Returns the currently active job (status `pending` or `running`) for the reel, falling back to the most recently created job when none are active. Used to track seamlessly through the enrich → generate job chain without a URL change.

**Response:** `text/html` — `fragments/pipeline_status.html`

**Errors:**
- `404` — reel not found, or the reel has no jobs at all

---

### `GET /api/reels/{reel_id}`

Render the full reel detail page: beat-by-beat production guide, hashtags, render/approve/publish controls for each cut, and a pipeline cost/latency/quality panel aggregated from `StageEvent`/`Job` rows.

The route precomputes, per cut: the live in-flight render/publish `Job` (so a fresh page load embeds the real self-polling status fragment instead of a static "refresh to update" message for a cut already `rendering`/`publishing`), and the most recently failed `Job` (so a `"failed"` cut or reel shows the real `job.error` instead of a generic message).

**Response:** `text/html` — `reel.html`

**Errors:**
- `404` — reel not found

---

## Jobs

### `GET /api/jobs/{job_id}`

Fetch job status as JSON — the only JSON endpoint in the app.

**Response:** `application/json`

```json
{
  "id": 42,
  "status": "running",
  "progress": 70,
  "error": null
}
```

**Status values:** `pending` | `running` | `done` | `failed`

**`error` field:** `null` on success; human-readable error string (truncated to 2000 chars, sanitized) on failure.

Quality score and context score on success are in `job.meta.quality_score` and `job.meta.context_score` respectively (not part of this JSON shape — read via the ORM/DB directly, or see them surfaced as "quality score NN/100 · context score NN/100" in the HTML `pipeline_status.html` fragment).

**Errors:**
- `404` — job not found

---

## Cuts

All `/cuts/{cut_id}/*` routes are defined in `api/routers/cuts.py`. Row-mutating routes (`render`, `PATCH`, `approve`, `publish`, `thumbnail`, `hook-variant`) take the cut row `SELECT ... FOR UPDATE` so a double-click or two concurrent actions on the same cut serialize instead of both passing the same status guard.

### `POST /api/cuts/{cut_id}/render`

Transition a cut to `rendering` and enqueue `render_cut`.

**Valid from statuses:** `draft`, `in_review` (re-render), `failed` (auto-resets to `draft` first) — except a cut that already has a `platform_post_id`, which is refused outright (re-rendering a posted cut would point it at stale assets while a later "finalize" publish never re-uploads).

**Response:** `text/html` — `fragments/render_status.html` with an HTMX polling trigger.

**Errors:**
- `404` — cut not found
- `422` — cut has no guide yet
- `409` — cut is already `rendering`, is in a non-renderable status, or is already posted (`platform_post_id` set)
- `503` — the job could not be queued (broker unreachable); the job and cut are rolled back so it can be retried at once

---

### `GET /api/cuts/{cut_id}/render-status`

HTMX polling endpoint for render progress.

**Query params:** `job_id` (required)

**Response:** `text/html` — `fragments/render_status.html`. When `done`, the fragment contains a `<video>` element, the thumbnail-candidate picker, the hook-variant picker, and a "Download captions (.srt)" link when `subtitle_path` is set. Polling stops when status is `done` or `failed`.

**Errors:**
- `404` — job or cut not found

---

### `PATCH /api/cuts/{cut_id}`

Edit a cut's guide beats, caption, and hashtags. Only allowed when the cut is `in_review`. Accepts `multipart/form-data`.

| Field | Type | Description |
|---|---|---|
| `caption` | string | Replacement caption (only applied if non-empty after `.strip()`) |
| `hashtags_raw` | string | Comma-separated hashtag list (no `#`; only applied if non-empty) |
| `beat_{i}_duration_s` | float | Duration override for beat `i`, seconds |
| `beat_{i}_visual_direction` | string | New stock-footage/photo search query for beat `i` |
| `beat_{i}_vo_script` | string | New voiceover script for beat `i` |
| `beat_{i}_on_screen_text` | string | Newline-separated text overlay lines for beat `i` (capped at 5, blanks stripped — not deduped) |

`i` is zero-indexed. Any subset of fields can be submitted — omitted fields keep existing values. Each beat field is only **written** when its normalized value genuinely differs from what's already stored (both sides normalized identically — CRLF/strip for `vo_script`, strip for `visual_direction`, strip-blanks+cap-5 for `on_screen_text`, plain compare for `duration_s`) via `engine/generation/guide_edit.py::set_beat_field()` — a full-form resubmit that touched nothing leaves `cut.guide` byte-for-byte unchanged, which matters because `Cut.rendered_guide_fingerprint`'s publish-time staleness comparison would otherwise be spuriously tripped by a pure caption-only save. The body is read **before** the row lock is taken, so a slow client never holds the lock (and a pool connection) while its request trickles in.

**Response:** `text/html` — `fragments/cut_card.html` replacing the whole cut card.

**Errors:**
- `404` — cut not found
- `409` — cut is not in `in_review` status

---

### `POST /api/cuts/{cut_id}/approve`

Transition a cut from `in_review` to `approved`.

**Response:** `text/html` — `fragments/cut_card.html` with updated status.

**Errors:**
- `404` — cut not found
- `409` — cut is not in `in_review` status

---

### `POST /api/cuts/{cut_id}/publish`

Transition a cut to `publishing` and enqueue `publish_cut`. The three publish-time gates (`assert_safe_to_publish`, `assert_video_matches_pins`, `assert_video_matches_guide` — see `docs/architecture.md`) run inside the task itself, not in this route.

**Valid from statuses:** `approved`, `scheduled`, `failed` (auto-resets to `approved` first — a publish failure doesn't need a re-render, unlike a render failure).

If the cut already has a `platform_post_id` (a previous run posted it but didn't finish recording that — reaped, or shut down before the done-stamp), the task finalizes without uploading again, and none of the three gates run on that branch.

**Response:** `text/html` — `fragments/publish_status.html` with an HTMX polling trigger.

**Errors:**
- `404` — cut not found
- `422` — cut has no rendered video
- `409` — cut is already `publishing`, or is in a non-publishable status
- `503` — the job could not be queued (broker unreachable); the job and cut are rolled back so it can be retried at once

---

### `GET /api/cuts/{cut_id}/publish-status`

HTMX polling endpoint for publish progress.

**Query params:** `job_id` (required)

**Response:** `text/html` — `fragments/publish_status.html`. Polling stops when status is `done` or `failed`.

**Errors:**
- `404` — job or cut not found

---

### `GET /api/cuts/{cut_id}/video`

Stream the rendered MP4 file.

**Response:** `video/mp4` — `FileResponse` with `Content-Disposition: attachment`, filename `reel_{reel_id}_{platform}.mp4`.

**Errors:**
- `404` — cut not found or not yet rendered
- `403` — resolved path falls outside `VIDEO_STORE_DIR`

---

### `GET /api/cuts/{cut_id}/subtitles`

Stream the SRT caption file `composite_cut()` writes alongside the MP4 (word-level Whisper transcription, or the proportional vo_script-sentence-split fallback when Whisper isn't installed). Same path-traversal guard as the video and thumbnail streams.

**Response:** `application/x-subrip` — `FileResponse` with `Content-Disposition: attachment`, filename `reel_{reel_id}_{platform}.srt`.

**Errors:**
- `404` — cut not found or `subtitle_path` unset (not yet rendered under this feature, or the render had nothing captionable — e.g. silent `voiceover_mode`)
- `403` — resolved path falls outside `VIDEO_STORE_DIR`

---

### `GET /api/cuts/{cut_id}/thumbnail/{index}`

Stream one of the render's sampled thumbnail candidate frames.

**Path params:** `index` — 0-based index into `cut.thumbnail_candidates`

**Response:** `image/jpeg`

**Errors:**
- `404` — cut not found, no thumbnail candidates, or `index` out of range
- `403` — resolved path falls outside `VIDEO_STORE_DIR`

---

### `POST /api/cuts/{cut_id}/thumbnail`

Choose one of the sampled thumbnail candidates as `cut.thumbnail_path`. Only allowed when the cut is `in_review`. Accepts `multipart/form-data`.

| Field | Type | Description |
|---|---|---|
| `index` | integer | 0-based index into `cut.thumbnail_candidates` |

**Response:** `text/html` — `fragments/cut_card.html`.

**Errors:**
- `404` — cut not found
- `409` — cut is not `in_review`
- `422` — `index` is not an integer, or out of range / no candidates exist

---

### `POST /api/cuts/{cut_id}/hook-variant`

Swap the hook beat's (`beats[0]`) `vo_script` for one of the generated alternates, re-deriving `on_screen_text` as a side effect. Only allowed when the cut is `in_review`. Accepts `multipart/form-data`.

| Field | Type | Description |
|---|---|---|
| `index` | integer | 0-based index into `cut.hook_variants` |

**Response:** `text/html` — `fragments/cut_card.html`.

**Errors:**
- `404` — cut not found
- `409` — cut is not `in_review`
- `422` — `index` is not an integer, out of range / no variants exist, or the cut's guide has no beats / beat 0 isn't a `"hook"` beat

---

## Credentials

All routes in `api/routers/credentials.py`. `provider` is always one of `SUPPORTED_PROVIDERS = ("youtube", "instagram")` — any other value 404s before anything else runs.

### `GET /api/credentials`

Connected-accounts page — connect/disconnect UI per provider. TikTok is shown as not-yet-available (no route exists for it; `TikTokPublisher` itself raises `NotImplementedError`).

**Response:** `text/html` — `credentials.html`

---

### `GET /api/credentials/{provider}/authorize`

Redirects the browser into the provider's OAuth2 consent screen, with a fresh CSRF `state` value (`api/oauth.py::new_state()`, process-local, single-operator tool).

**Response:** `302` redirect to the provider's authorization URL.

**Errors:**
- `404` — unknown provider
- `422` — that provider's OAuth client id/secret isn't configured in `.env`

---

### `GET /api/credentials/{provider}/callback`

OAuth2 callback. Exchanges the authorization code for tokens; for Instagram, additionally swaps to a long-lived token and discovers the connected Facebook Page's IG Business Account + Page access token (publishing rides on the Page token, not the user token). Upserts one `Credential` row per provider (encrypted at rest).

**Query params:** `code`, `state` (both required unless `error` is present), `error` (set by the provider on a denied consent)

**Response:** `303` redirect to `/api/credentials` on success.

**Errors:**
- `404` — unknown provider
- `400` — the provider returned `error`; missing `code`/`state`; `state` doesn't match what `authorize` issued (invalid or expired CSRF state)
- `502` — token exchange, long-lived-token exchange, or account discovery with the provider failed; or the provider returned no access token

---

### `POST /api/credentials/{provider}/disconnect`

Deletes the stored `Credential` row for that provider, if any.

**Response:** `303` redirect to `/api/credentials`.

**Errors:**
- `404` — unknown provider

---

## Insights

All routes in `api/routers/insights.py`.

### `GET /api/insights`

Quality↔engagement correlation (Pearson `r` + sample size, always shown together with a restriction-of-range caveat — never `r` alone; refuses to compute below `MIN_SAMPLE=5` reels or on zero variance in either series) + a top/bottom-3 performer table (or one combined list when fewer than `2 × MIN_SAMPLE` reels have both a quality score and views — hook line sourced from the max-views cut) + the `PerformanceNote` list/form.

**Response:** `text/html` — `insights.html`

---

### `POST /api/insights/notes`

Create a new, active `PerformanceNote`. Accepts `multipart/form-data`.

| Field | Type | Description |
|---|---|---|
| `text` | string | Required; a blank/whitespace-only value is silently ignored (no row created, no error) |

**Response:** `text/html` — `fragments/performance_notes.html` (the full current list).

---

### `POST /api/insights/notes/{note_id}/toggle`

Flip a note's `active` flag — active notes are seeded into `generate_guide`'s standard-LLM-path `prior_feedback`; inactive ones are excluded without being deleted.

**Response:** `text/html` — `fragments/performance_notes.html`

**Errors:**
- `404` — note not found

---

### `DELETE /api/insights/notes/{note_id}`

Hard-delete a note (no soft-delete/undo).

**Response:** `text/html` — `fragments/performance_notes.html`. A missing `note_id` is a silent no-op (not a 404) — the response still reflects the current list either way.

---

## UI routes (not prefixed with `/api`)

### `GET /`

Context-entry form page (`index.html`) — niche/platform/voiceover/target-length/generation-path selects, plus the curated TTS-voice and text-color pickers, and a live cost/time estimate wired to `POST /api/reels/estimate`.

---

## Status badge CSS classes

The HTML fragments use `.badge-{status}` classes (`ui/static/main.css`) for color coding, covering both `Job` and `Reel`/`Cut` status enums:

| Status | Class | Color |
|---|---|---|
| `pending` | `badge-pending` | Grey |
| `running` | `badge-running` | Amber |
| `done` | `badge-done` | Green |
| `failed` | `badge-failed` | Red |
| (quality-score warning) | `badge-warn` | Yellow, `cursor: help` |
| `draft` | `badge-draft` | Grey |
| `enriching` | `badge-enriching` | Amber |
| `generating` | `badge-generating` | Amber |
| `guide_ready` | `badge-guide_ready` | Blue |
| `rendering` | `badge-rendering` | Amber |
| `in_review` | `badge-in_review` | Blue |
| `approved` | `badge-approved` | Green |
| `scheduled` | `badge-scheduled` | Purple |
| `publishing` | `badge-publishing` | Amber |
| `published` | `badge-published` | Green |

---

## Error handling

- LLM validation errors and quality evaluation failures are caught in `generate_guide` and stored in `jobs.error` (truncated to 2000 chars, NUL bytes stripped, lone surrogates replaced, SQLAlchemy `[parameters: …]` redacted)
- Quality failures include the final score and per-axis issues: `"Guide quality score 42/100 after 3 attempts — Issues: Script coverage 20% …"`
- Render and publish exceptions are caught by their respective tasks and stored the same way
- The render/publish status fragment displays `job.error` when status is `failed`; a fresh page load of the reel/cut (not just the tab that triggered the job) also shows it, via `latest_failed_job_for_cut()`/`latest_failed_job_for_reel()`
- HTTP 4xx/5xx responses from Pexels, Wikipedia, HuggingFace, Edge TTS, the LLM, or a publish platform's API surface as job failures (not HTTP errors to the browser) — except OAuth connect/callback errors in `credentials.py`, which do return real HTTP error codes directly, since that flow is itself a synchronous request/response, not a background job
- Transient failures (connection errors, timeouts, HTTP 429/5xx) are retried twice with 30 s/60 s backoff before the job is failed (`generate_guide` and `render_cut` only — `enrich_context` and `publish_cut` never retry automatically: an enrichment retry re-runs a paid call for no gain, and a publish retry after an accepted upload could post twice). During backoff the job sits at `pending` with `error` set to `"transient failure, retry N: ..."`; the message is cleared if a later attempt succeeds
- Wikimedia CDN 429 rate-limits are handled silently (retry + fallback) and never fail the job; the beat falls through the asset chain instead
- A router whose `.delay()` call itself raises (broker unreachable) fails the job immediately (`fail_unenqueued`) and returns `503` to the browser, rather than leaving a `pending` job for the reaper's much longer (240 min) staleness window to eventually catch
