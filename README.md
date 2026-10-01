# Reel Maker

Local-first, single-operator tool that turns a topic into a publishable faceless short-form video. You enter context, it generates a production guide with an LLM, sources stock footage, synthesizes voiceover, and renders platform-specific 9:16 MP4s for YouTube Shorts, Instagram Reels, and TikTok (render/review only — see Publishing below).

## How it works

```
Context entry → context scoring/enrichment → LLM guide generation → quality evaluation
             → stock footage/photos/AI assets + TTS + music → MoviePy render → Review → Publish
```

Everything slow runs in a background Celery worker. The browser UI polls for progress and shows results without a page reload.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | ≥ 3.11 | |
| Docker | any recent | Postgres + Redis via `docker compose` |
| FFmpeg | any recent | Required by MoviePy for encoding |
| Ollama | latest | Serves the local LLM. Optional if you use NVIDIA NIM instead |
| Pexels API key | — | Free at pexels.com/api — for stock footage |
| NVIDIA NIM key | — | Optional but recommended. Free at build.nvidia.com — used for enrichment, the quality judge, and (optionally) generation |

Install FFmpeg on macOS: `brew install ffmpeg`

---

## Setup

### 1. Clone and install

```bash
git clone <repo-url> reel-maker
cd reel-maker

python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"     # [dev] adds pytest
.venv/bin/pip install edge-tts        # voiceover — see TTS note below
```

### 2. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in at minimum:

```env
PEXELS_API_KEY=your_key_here
NVIDIA_API_KEY=your_key_here   # optional; falls back to local Ollama when blank
```

Everything else has working defaults for local dev. See [Publishing setup](#publishing-setup) below before you try to connect YouTube/Instagram.

### 3. Start infrastructure

```bash
docker compose up -d       # starts Postgres on :5432 and Redis on :6379
```

### 4. Run database migrations

```bash
.venv/bin/alembic upgrade head
```

### 5. Pull the LLM model

```bash
ollama pull qwen3:14b      # ~9 GB download; runs locally via Ollama
```

A smaller model works if VRAM is tight — change `LLM_MODEL` in `.env`. The 3.2B `llama3.2` model is too small and fails schema validation; test any new model with one generation before relying on it.

---

## Running the app

Tasks are routed to two named queues, so a worker started without `-Q` consumes
**nothing**. Open **four terminal tabs** from the project root (five with local Ollama):

**Tab 1 — API server**
```bash
.venv/bin/uvicorn api.main:app --reload
```

**Tab 2 — Generation worker** (LLM, enrich, render-adjacent, publish, and metrics-pull tasks — I/O-bound)
```bash
.venv/bin/celery -A worker.celery_app worker -Q generation -c 4 -l info
```

**Tab 3 — Render worker** (ffmpeg, CPU-bound — keep concurrency at 1)
```bash
.venv/bin/celery -A worker.celery_app worker -Q rendering --concurrency=1 -l info
```

**Tab 4 — Beat scheduler** (stuck-job reaper every 60s, metrics pull every 6h)
```bash
.venv/bin/celery -A worker.celery_app beat -l info
```

**Tab 5 — LLM** (skip if using NVIDIA NIM)
```bash
ollama serve
```

Open `http://localhost:8000` in your browser.

### Alternative: everything in Docker

`docker-compose.yml` also defines `api`, `worker-generation`, `worker-rendering`,
and `beat` services (in addition to `postgres`/`redis`) — one image
(`Dockerfile`), built once, run four ways via `command:` overrides. This is a
real deploy path, not just the dev database:

```bash
cp .env.example .env              # fill in at minimum PEXELS_API_KEY + NVIDIA_API_KEY
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head   # once, before first `up` — see the
                                                     # comment in docker-compose.yml for why
                                                     # this can't be baked into a container's
                                                     # own startup (N workers would race it)
docker compose up -d
```

`http://localhost:8000` as above. `ollama serve` still runs on the host (skip
entirely if using NVIDIA NIM) — it isn't containerized here, since most local
setups already run it that way. The image installs `edge-tts` for you, so
voiceover works out of the box in Docker without a separate install step.

---

## Using the app

### Generate a guide

1. Enter a **context** — what the reel is about. Be specific: *"5 money habits that helped me save $10k in one year"* beats *"saving money"*.
2. Set a **niche** (e.g. `personal finance`).
3. Choose **platforms** — YouTube Shorts and Instagram Reels are checked by default; TikTok is selectable too, but publishing isn't implemented for it (render/review only — see [Publishing](#publishing) below).
4. Choose **voiceover mode**: `Voiceover` (AI voice), `Music only`, or `Silent`.
5. Pick a **voice** (voiceover mode only) from a curated list of edge-tts neural voices (British/American/Australian/Irish/Indian, male/female) — or leave it on `Default` (`en-GB-RyanNeural`). This only applies when `TTS_PROVIDER=edge`; Kokoro always uses its own default voice.
6. Pick a **text color** for on-screen captions from a curated list (white/yellow/cyan/red/orange/black) — or leave it on `Default` (white).
7. Choose a **target length**: 30s, 45s, 60s, 75s, or 90s.
8. Choose a **generation path**: `Auto-detect`, `I wrote a script` (fast structured path, ~90 s), or `Generate from topic` (full LLM, 2–5 min).
9. Click **Generate guide**.

The input is first scored for engagement potential (0–100) and enriched by an LLM if it scores below 60 — skipped automatically when you supply a structured script, since enrichment would drift off topic.

The page polls in the background. When the job completes, a "View guide →" link appears.

### Review the guide

The guide page shows a beat-by-beat production plan for each platform cut (one `Cut` row per selected platform), including:
- Voiceover script per beat
- Stock footage search query per beat (`visual_direction`)
- On-screen text overlays
- Platform caption and hashtags

### Render a video

Click **Render** on any cut. The worker will:
1. Source visuals per beat through a fallback chain: Wikipedia player/subject photos (when a full name is named in the visual direction) → Pexels stock footage → HuggingFace-generated video → HuggingFace-generated image → a black frame as a last resort (see [AI-generated asset fallback](#ai-generated-asset-fallback-huggingface) below)
2. Synthesize voiceover (requires optional TTS install — see below)
3. Mix in background music from your local library, if configured (see [Background music](#background-music-local-library))
4. Composite clips, text overlays, and audio with MoviePy; a beat naming two people (two resolved photos) renders as a side-by-side 2-up collage instead of cycling sequentially
5. Export a 1080×1920 MP4 at 30 fps, plus a `.srt` caption file (see [Captions](#captions--subtitles))

A progress bar polls every 3 seconds. When rendering completes, an inline video player appears, with **Download MP4** and (when captions were produced) **Download captions (.srt)** links underneath. If any beat found no real footage/photo anywhere in the fallback chain, a warning banner lists which beats rendered as a black frame.

The render also samples 4 thumbnail candidate frames and, if the guide cleared the quality gate, generates 3 alternate hook-line variants — both pickable from the `in_review` card (see below).

### Re-render

Renders can be triggered again from the `in_review` state — click **Re-render** below the video player. This re-sources footage (reusing any beat whose `visual_direction` hasn't changed) and re-runs the full compositing pipeline. A re-render replaces the thumbnail candidates and any picked hook variant's effect wholesale, same as the video file itself.

### Pick a thumbnail or hook variant

While a cut is `in_review`:
- **Thumbnail** — click any of the 4 sampled thumbnail frames shown under the video to make it the cut's thumbnail. The currently chosen one is outlined.
- **Hook alternates** — if hook-variant generation succeeded, up to 3 alternate opening lines are listed above the caption editor. Clicking one swaps beat 0's voiceover script to that line (and re-derives its on-screen text) without a re-render.

Both actions are gated to `in_review` — they're not available once a cut is approved or published.

### Edit and approve

While `in_review`, the caption, hashtags, and every beat field (duration, voiceover script, visual direction, on-screen text) are editable inline. Click **Save changes** to persist an edit, then **Approve ✓** when the cut is ready to render into a final video (or publish, if it's already rendered how you want).

### Publishing

Once a cut is `approved`, click **Publish**. The worker will:
1. Enforce the `safe_to_publish` gate — any bound asset (typically a Wikipedia photo without a clear license) blocks the publish with an actionable error instead of risking a copyright issue.
2. Check that the rendered video still matches the cut's current pinned assets and current guide content (a guide edit or re-pin followed by a failed re-render is caught here rather than silently shipping a stale video).
3. Upload via the platform's publisher (YouTube Data API resumable upload, or Instagram Graph API container create/poll/publish) using a caption built from the saved caption plus a Wikipedia attribution block, if any assets need it.
4. Commit the platform's post ID the moment the upload succeeds — a retried publish on an already-posted cut finalizes without re-uploading.

A cut that fails to publish lands in `failed` with the real error shown on the card; "Retry publish" tries again without re-rendering (unless the video is now stale relative to the guide, in which case you're asked to re-render first). **TikTok publishing is not implemented on purpose** — `TikTokPublisher.publish()` raises an explicit error; TikTok cuts can be rendered and reviewed but must be uploaded to the platform manually. Once published, the cut card shows views/likes/comments, refreshed automatically every 6 hours (`pull_publish_metrics`) once the platform allows it; before the first pull it shows "checked every 6h".

#### Publishing setup

Publishing to YouTube or Instagram needs OAuth app credentials, connected from **Connected accounts** (linked in the top nav, `/api/credentials`):

1. **YouTube** — register an app at [console.cloud.google.com](https://console.cloud.google.com), enable the YouTube Data API v3, and add `{PUBLIC_BASE_URL}/api/credentials/youtube/callback` as an authorized redirect URI. Set `YOUTUBE_OAUTH_CLIENT_ID` / `YOUTUBE_OAUTH_CLIENT_SECRET` in `.env`.
2. **Instagram** — register an app at [developers.facebook.com](https://developers.facebook.com). You need a Facebook Page linked to an Instagram Business/Creator account. Add `{PUBLIC_BASE_URL}/api/credentials/instagram/callback` as a valid OAuth redirect URI. Set `META_OAUTH_APP_ID` / `META_OAUTH_APP_SECRET` in `.env`.
3. **`PUBLIC_BASE_URL`** must be a real, publicly reachable **HTTPS** URL, not `localhost` — it's used both to build the OAuth redirect URI above and, for Instagram specifically, as the base of the video URL Instagram's Graph API fetches directly from your server (Instagram does not accept an uploaded video body the way YouTube does). For local development, tunnel your server with something like `ngrok http 8000` and set `PUBLIC_BASE_URL` to the generated HTTPS URL.
4. Click **Connect** next to each provider on the Connected accounts page to run the OAuth flow; **Disconnect** removes the stored credential. A connected card shows the account label and token expiry when known.
5. Credentials are encrypted at rest with `CREDENTIALS_KEY` (a Fernet key) — see the config reference below. Without it, tokens are stored as plaintext with a warning (fine for local dev, not for anything shared).

### Insights

The **Insights** page (linked in the top nav, `/api/insights`) has three sections:

1. **Quality ↔ engagement correlation** — a Pearson *r* between each reel's quality score and its max cut views, always shown together with the sample size (`n = ...`), never *r* alone. It refuses to compute below 5 measured reels, or when either series has zero variance, and always shows a caveat that quality scores cluster near the acceptance threshold by construction, which weakens any correlation this can detect — correlation, not causation.
2. **Top / bottom performers** — a table of your best- and worst-performing published reels by views (combined into one table if you don't have enough data yet for a meaningful split), each row showing quality score, views, and the hook line from whichever cut drew the most views. This is meant to inform performance notes below — raw reel content is never fed back into generation automatically.
3. **Performance notes** — plain-English notes you write yourself (e.g. *"Hooks phrased as a direct question outperform statement hooks"*). Every *active* note is seeded into the standard LLM generation path's feedback from attempt 1 onward (structured-script generation doesn't use this). Toggle a note's checkbox to activate/deactivate it, or click × to delete it — both update immediately.

### Reel list and pipeline panel

`GET /api/reels` lists every reel with status, per-cut platform badges, latest quality score, and max views. Clicking into a reel (`GET /api/reels/{id}`) shows a pipeline cost/latency/quality panel sourced from `StageEvent` rows (every LLM call, asset-generation call, and publish-adjacent call this reel has made), followed by each cut's card.

---

## Voiceover

`TTS_PROVIDER` defaults to `edge` — Microsoft Edge neural voices, free, no GPU, Python 3.14 compatible:

```bash
.venv/bin/pip install edge-tts
```

If `edge-tts` is not installed the provider falls back to `SilentProvider`, which emits a
1-second silence stub per beat and logs a warning. Renders still complete, but with no audio.

Synthesized audio is cached by `sha256(voice + rate + text)`. `synth_to_budget()` re-synthesizes
at an adjusted speaking rate (clamped ±25%) when a beat's measured duration drifts more than 15%
from its target.

Per-reel voice selection (the **Voice** dropdown on the create-reel form) only applies to the `edge` provider — its curated voice IDs (e.g. `en-US-JennyNeural`) are a different namespace than Kokoro's (e.g. `af_heart`), so a chosen edge voice is never forwarded to Kokoro.

**Kokoro** (`TTS_PROVIDER=kokoro`) is an alternative local neural TTS with higher quality, but
requires **Python < 3.13**:

```bash
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -e ".[tts]"
```

---

## Captions / subtitles

Whisper transcription (see below) is optional, but when it's available the compositor uses the word-level timestamps to also write a `.srt` file alongside the MP4 — downloadable from the cut card once a render completes (**Download captions (.srt)**). Without Whisper, timing falls back to a proportional, sentence-based estimate, which also still produces an `.srt`.

If a YouTube cut is published and has a caption file, `YouTubePublisher` makes one best-effort attempt to upload it via the YouTube Captions API (`captions.insert`) right after the video upload succeeds — this never fails the publish itself if it doesn't work, it's recorded as its own instrumentation event either way. This requires the `youtube.force-ssl` OAuth scope in addition to `youtube.upload`; if you connected YouTube before this was added, disconnect and reconnect to pick up the wider grant.

---

## Optional: Word-level captions with Whisper

```bash
.venv/bin/pip install -e ".[captions]"
# ffmpeg must be on PATH
```

When installed, the compositor transcribes each beat's audio and drives on-screen text timing from
word-level timestamps instead of proportional word-count estimates, and produces the `.srt` caption
file from the same transcription pass. Without it, timing falls back to proportional automatically
and captions are still produced from the fallback sentence-split — no configuration needed either way.

---

## AI-generated asset fallback (HuggingFace)

When a beat's visual direction finds nothing on Wikipedia or Pexels, set `HUGGINGFACE_API_KEY` in `.env` to let the asset sourcer generate media instead of falling straight to a black frame:

```env
HUGGINGFACE_API_KEY=your_key_here
```

- `HuggingFaceVideoSource` generates a short clip with LTX-Video (`HUGGINGFACE_VIDEO_MODEL`, default `Lightricks/LTX-Video`).
- `HuggingFaceImageSource` generates a static image with FLUX.1-schnell (`HUGGINGFACE_IMAGE_MODEL`, default `black-forest-labs/FLUX.1-schnell`) as a further fallback if video generation also comes up empty.
- Full chain per beat: **Wikipedia → Pexels → HF Video → HF Image → black frame**.
- Both HF sources cache results locally by prompt fingerprint, so repeated/re-renders don't re-generate the same asset.
- Generation cost is tracked per call (not per cache hit) as `StageEvent` rows, visible in a reel's pipeline panel, using `HUGGINGFACE_PRICE_PER_IMAGE` / `HUGGINGFACE_PRICE_PER_VIDEO_SECOND` — both default to `0.0` (cost tracking inert) until you set them from your actual HF billing plan.
- All HF-generated assets are marked `safe_to_publish=True`.

---

## Background music (local library)

Reel Maker does not call any paid music API — `music_cue` (a short mood description per guide, e.g. "tense, minimal") is matched by keyword overlap against filenames in a local directory you populate yourself:

```env
MUSIC_LIBRARY_DIR=./data/music
```

Drop royalty-free tracks there with mood words in the filename, e.g. `tense_minimal_01.mp3`, `upbeat_energetic_02.mp3` — sourced yourself from somewhere like Pixabay's music page (browser-only, no API), the Free Music Archive, or your own library. An empty or missing directory just means no music gets mixed in, same as today. When a track matches, the render mixes it in with sidechain ducking under the voiceover (or a plain lower-volume mix when there's no voiceover track).

---

## Configuration reference

All values are read from `.env` (or environment variables). Defaults below are the real `pydantic-settings` field defaults in `api/config.py`.

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://reelmaker:reelmaker@localhost:5432/reelmaker` | Postgres connection string (explicit `+psycopg2` driver — the project depends on `psycopg2-binary`, not `psycopg` v3) |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker and result backend |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible LLM endpoint (Ollama) |
| `LLM_MODEL` | `qwen3:14b` | Main model for full guide generation and visuals prompts |
| `LLM_ENRICHMENT_MODEL` | `qwen3:14b` | Local model for enrichment + quality judge |
| `NVIDIA_API_KEY` | *(empty)* | When set, enrichment + judge calls route to NVIDIA NIM instead of local Ollama |
| `NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` | NVIDIA NIM API base URL |
| `NVIDIA_ENRICHMENT_MODEL` | `nvidia/nemotron-3-super-120b-a12b` | Hosted enrichment/judge model |
| `NVIDIA_GENERATION_MODEL` | `nvidia/nemotron-3-super-120b-a12b` | Hosted generation model |
| `USE_NVIDIA_FOR_GENERATION` | `false` | Route main guide generation to NVIDIA NIM too (higher measured quality, 81/100 vs 50/100) |
| `TTS_PROVIDER` | `edge` | `edge` (needs `edge-tts`), `kokoro` (Python < 3.13), or `silent` |
| `ASSET_STORE_DIR` | `./data/assets` | Where footage clips, photos, and TTS cache are stored |
| `VIDEO_STORE_DIR` | `./data/videos` | Where rendered MP4s, thumbnails, and caption files are stored |
| `PEXELS_API_KEY` | *(empty)* | Stock footage; next in the fallback chain is HuggingFace, then a black frame |
| `PIXABAY_API_KEY` | *(empty)* | Reserved, not yet wired in — Pixabay's public REST API has no documented Music endpoint; see Background music above |
| `HUGGINGFACE_API_KEY` | *(empty)* | Enables AI-generated video/image fallback when Wikipedia and Pexels find nothing — see above |
| `HUGGINGFACE_IMAGE_MODEL` | `black-forest-labs/FLUX.1-schnell` | Model for HF image generation |
| `HUGGINGFACE_VIDEO_MODEL` | `Lightricks/LTX-Video` | Model for HF video generation |
| `HUGGINGFACE_PRICE_PER_IMAGE` | `0.0` | USD per HF image generation, for cost tracking — set from your real HF billing plan |
| `HUGGINGFACE_PRICE_PER_VIDEO_SECOND` | `0.0` | USD per second of HF video generation, for cost tracking |
| `MUSIC_LIBRARY_DIR` | `./data/music` | Local royalty-free music library for background mixing — see above |
| `MAX_PAID_LLM_CALLS_PER_REEL` | `20` | Hard ceiling on paid (NVIDIA NIM) LLM calls per reel, as a backstop against a stuck retry loop |
| `NVIDIA_PRICE_PER_1M_INPUT_TOKENS` | `0.0` | USD per 1M input tokens for NVIDIA NIM cost tracking — set from your real billing plan |
| `NVIDIA_PRICE_PER_1M_OUTPUT_TOKENS` | `0.0` | USD per 1M output tokens for NVIDIA NIM cost tracking |
| `CREDENTIALS_KEY` | *(empty)* | Fernet key encrypting `credentials.token_blob`/`refresh_token_blob`; plaintext with a warning when blank |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Publicly reachable HTTPS URL for this server — builds the OAuth redirect URI and, for Instagram, the video URL it fetches. Required for real OAuth/Instagram publishing — see Publishing setup above |
| `YOUTUBE_OAUTH_CLIENT_ID` | *(empty)* | YouTube Data API OAuth client ID |
| `YOUTUBE_OAUTH_CLIENT_SECRET` | *(empty)* | YouTube Data API OAuth client secret |
| `META_OAUTH_APP_ID` | *(empty)* | Meta/Instagram Graph API OAuth app ID |
| `META_OAUTH_APP_SECRET` | *(empty)* | Meta/Instagram Graph API OAuth app secret |
| `EVALUATOR_AXIS_WEIGHT_MULTIPLIERS` | `{}` | JSON object scaling individual evaluator axis deductions, e.g. `'{"insight": 0.5}'` — a manual lever informed by the Insights correlation data, not auto-tuned |

---

## Development

```bash
# Run all tests (821 tests; 1 more — the `golden` marker — is deselected by default)
.venv/bin/pytest

# Run the golden-reel smoke test too (real edge-tts + real ffmpeg, ~20s, needs network)
.venv/bin/pytest -m golden

# Single test
.venv/bin/pytest tests/test_foo.py::test_bar -s

# Create a new Alembic migration after changing models.py
.venv/bin/alembic revision --autogenerate -m "describe the change"
.venv/bin/alembic upgrade head
```

See [`docs/architecture.md`](docs/architecture.md) for a full technical deep-dive, [`docs/api.md`](docs/api.md) for every HTTP endpoint, [`docs/data-model.md`](docs/data-model.md) for the full schema and state machines, and [`docs/roadmap.md`](docs/roadmap.md) for what's shipped vs. not yet wired in.
