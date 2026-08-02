# API Reference

All endpoints are prefixed with `/api`. The browser receives HTML fragments (Jinja2 templates via HTMX); programmatic callers can use the JSON endpoints.

---

## Reels

### `POST /api/reels`

Create a reel and enqueue guide generation. Accepts `multipart/form-data`.

**Form fields:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `context` | string | yes | — | Topic/prompt for the reel |
| `niche` | string | no | `null` | Content category |
| `voiceover_mode` | string | no | `voiceover` | `voiceover` \| `music_only` \| `silent` |
| `target_length_s` | float | no | `45.0` | Target video length in seconds (30/45/60/75/90) |
| `generation_path` | string | no | `auto` | `auto` \| `structured` \| `standard` — see guide generation paths |

**Response:** `text/html` — `fragments/pipeline_status.html` fragment polling `GET /api/reels/{reel_id}/active-job-fragment` every 2 s.

---

### `GET /api/reels/{reel_id}/active-job-fragment`

HTMX polling endpoint. Returns the currently active job (status `pending` or `running`) for the reel. Falls back to the most recent job when none are active. Used to track seamlessly through the enrich → generate job chain without a URL change.

**Response:** `text/html` — `fragments/pipeline_status.html`

---

### `GET /api/reels/{reel_id}`

Render the guide detail page for a reel.

**Response:** `text/html` — `reel.html` with beat-by-beat production guide, hashtags, and render controls for each cut.

---

## Jobs

### `GET /api/jobs/{job_id}`

Fetch job status as JSON.

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

**`error` field:** `null` on success; human-readable error string (truncated to 2000 chars) on failure.

Quality score and context score on success are in `job.meta.quality_score` and `job.meta.context_score` respectively, surfaced as "quality score NN/100 · context score NN/100" in the pipeline status fragment.

---

## Cuts

### `PATCH /api/cuts/{cut_id}`

Edit a cut's guide, caption, and hashtags. Only allowed when cut is in `in_review` status.

Accepts `multipart/form-data`:

| Field | Type | Description |
|---|---|---|
| `caption` | string | Replacement caption |
| `hashtags_raw` | string | Comma-separated hashtag list (no `#`) |
| `beat_{i}_duration_s` | float | Duration override for beat `i` in seconds (min 1, max 30); step 0.5 |
| `beat_{i}_visual_direction` | string | New stock footage search query for beat `i` |
| `beat_{i}_vo_script` | string | New voiceover script for beat `i` |
| `beat_{i}_on_screen_text` | string | Newline-separated text overlay lines for beat `i` (max 5) |

`i` is zero-indexed. Any subset of fields can be submitted — omitted fields keep existing values.

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

### `POST /api/cuts/{cut_id}/render`

Transition a cut to `rendering` and enqueue the render task.

**Valid from statuses:** `draft`, `in_review` (re-render), `failed` (auto-resets to `draft` first)

**Response:** `text/html` — `fragments/render_status.html` with HTMX polling trigger.

**Errors:**
- `404` — cut not found
- `409` — cut is already `rendering`, or is in a non-renderable status

---

### `GET /api/cuts/{cut_id}/render-status`

HTMX polling endpoint for render progress.

**Query params:** `job_id` (required)

**Response:** `text/html` — `fragments/render_status.html`. When `done`, the fragment contains a `<video>` element. Polling stops when status is `done` or `failed`.

---

### `GET /api/cuts/{cut_id}/video`

Stream the rendered MP4 file.

**Response:** `video/mp4` — `FileResponse` with `Content-Disposition: attachment` header.

**Errors:**
- `404` — cut not found or not yet rendered

---

## UI routes (not prefixed with `/api`)

### `GET /`

Context-entry form page.

---

## Status badge CSS classes

The HTML fragments use `.badge-{status}` classes for color coding:

| Status | Class | Color |
|---|---|---|
| `pending` | `badge-pending` | Grey |
| `running` | `badge-running` | Amber |
| `done` | `badge-done` | Green |
| `failed` | `badge-failed` | Red |
| `draft` | `badge-draft` | Grey |
| `rendering` | `badge-rendering` | Amber |
| `in_review` | `badge-in_review` | Blue (inherited from running style) |
| `guide_ready` | `badge-guide_ready` | Green |

---

## Error handling

- LLM validation errors and quality evaluation failures are caught in `generate_guide` and stored in `jobs.error` (truncated to 2000 chars)
- Quality failures include the final score and per-axis issues: `"Guide quality score 42/100 after 3 attempts — Issues: Script coverage 20% …"`
- Render exceptions are caught in `render_cut` and stored similarly
- The job fragment displays `job.error` when status is `failed`
- HTTP 4xx/5xx responses from Pexels, Wikipedia, Edge TTS, or the LLM surface as job failures (not HTTP errors to the browser)
- Wikimedia CDN 429 rate-limits are handled silently (retry + thumbnail fallback) and do not fail the job; the beat falls back to Pexels stock footage
