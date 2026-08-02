# Reel Maker

Local-first, single-operator tool that turns a topic into a publishable faceless short-form video. You enter context, it generates a production guide with an LLM, sources stock footage, synthesizes voiceover, and renders platform-specific 9:16 MP4s for YouTube Shorts and Instagram Reels.

## How it works

```
Context entry → LLM guide generation → Stock footage + TTS → MoviePy render → Review → Publish
```

Everything slow runs in a background Celery worker. The browser UI polls for progress and shows results without a page reload.

---

## Prerequisites

| Requirement | Version | Notes |
|---|---|---|
| Python | ≥ 3.11 | |
| Docker | any recent | Postgres + Redis via `docker compose` |
| FFmpeg | any recent | Required by MoviePy for encoding |
| Ollama | latest | Serves the local LLM |
| Pexels API key | — | Free at pexels.com/api — for stock footage |

Install FFmpeg on macOS: `brew install ffmpeg`

---

## Setup

### 1. Clone and install

```bash
git clone <repo-url> reel-maker
cd reel-maker

python3 -m venv .venv
.venv/bin/pip install -e "."
```

### 2. Configure environment

```bash
cp .env.example .env
```

Open `.env` and fill in at minimum:

```env
PEXELS_API_KEY=your_key_here
```

Everything else has working defaults for local dev.

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

A smaller model works if VRAM is tight — change `LLM_MODEL` in `.env`.

---

## Running the app

Open **three terminal tabs** from the project root:

**Tab 1 — API server**
```bash
.venv/bin/uvicorn api.main:app --reload
```

**Tab 2 — Background worker**
```bash
.venv/bin/celery -A worker.celery_app worker -l info
```

**Tab 3 — LLM (if not already running)**
```bash
ollama serve
```

Open `http://localhost:8000` in your browser.

---

## Using the app

### Generate a guide

1. Enter a **context** — what the reel is about. Be specific: *"5 money habits that helped me save $10k in one year"* beats *"saving money"*.
2. Set a **niche** (e.g. `personal finance`).
3. Choose **voiceover mode**: `Voiceover` (AI voice), `Music only`, or `Silent`.
4. Choose a **target length**: 30s, 45s, or 60s.
5. Click **Generate guide**.

The page polls in the background. When the job completes, a "View guide →" link appears.

### Review the guide

The guide page shows a beat-by-beat production plan for each platform cut (YouTube Shorts + Instagram Reels), including:
- Voiceover script per beat
- Stock footage search query per beat
- On-screen text overlays
- Platform caption and 15 hashtags

### Render a video

Click **Render** on any cut. The worker will:
1. Download portrait stock clips from Pexels matching each beat's `visual_direction`
2. Synthesize voiceover (requires optional TTS install — see below)
3. Composite clips, text overlays, and audio with MoviePy
4. Export a 1080×1920 MP4 at 30 fps

A progress bar polls every 3 seconds. When rendering completes, an inline video player appears. Click **Download MP4** to save the file.

### Re-render

Renders can be triggered again from the `in_review` state — click **Re-render** below the video player. This re-sources footage and re-runs the full compositing pipeline.

---

## Optional: Voiceover with Kokoro TTS

By default, renders produce silent voiceover (a 1-second silence stub per beat). To enable real AI voiceover:

```bash
# Kokoro requires PyTorch — install CPU build first if you don't have a GPU:
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -e ".[tts]"
```

Then set in `.env`:

```env
TTS_PROVIDER=kokoro
```

Kokoro downloads model weights on first use (~500 MB). Subsequent synths for the same text are cached by hash.

---

## Optional: Word-level captions with Whisper

```bash
.venv/bin/pip install -e ".[captions]"
# ffmpeg must be on PATH
```

Whisper is installed but not yet wired into the compositor (Phase 2 deliverable). The `engine/render/captions.py` module is ready to call — use `transcribe_audio(path, beat_offset_s)` to get `CaptionSegment` objects with word-level timestamps.

---

## Configuration reference

All values are read from `.env` (or environment variables):

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://reelmaker:reelmaker@localhost:5432/reelmaker` | Postgres connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker and result backend |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible LLM endpoint (Ollama) |
| `LLM_MODEL` | `qwen3:14b` | Model name passed to the LLM endpoint |
| `TTS_PROVIDER` | `silent` | `silent` (no deps) or `kokoro` |
| `PEXELS_API_KEY` | *(empty)* | Required for stock footage; falls back to black frames |
| `PIXABAY_API_KEY` | *(empty)* | Alternative footage source (not yet wired in) |
| `ASSET_STORE_DIR` | `./data/assets` | Where footage clips and TTS cache are stored |
| `VIDEO_STORE_DIR` | `./data/videos` | Where rendered MP4s and thumbnails are stored |

---

## Development

```bash
# Run all tests
.venv/bin/pytest

# Single test
.venv/bin/pytest tests/test_foo.py::test_bar -s

# Create a new Alembic migration after changing models.py
.venv/bin/alembic revision --autogenerate -m "describe the change"
.venv/bin/alembic upgrade head
```

See [`docs/architecture.md`](docs/architecture.md) for a full technical deep-dive.
