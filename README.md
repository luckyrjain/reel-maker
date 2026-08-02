# Reel Maker

Local-first, single-operator tool that turns a topic into a publishable faceless short-form video. You enter context, it generates a production guide with an LLM, sources stock footage, synthesizes voiceover, and renders platform-specific 9:16 MP4s for YouTube Shorts and Instagram Reels.

## How it works

```
Context entry → context scoring/enrichment → LLM guide generation → quality evaluation
             → stock footage + Wikipedia photos + TTS → MoviePy render → Review → Publish
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

Tasks are routed to two named queues, so a worker started without `-Q` consumes
**nothing**. Open **four terminal tabs** from the project root (five with local Ollama):

**Tab 1 — API server**
```bash
.venv/bin/uvicorn api.main:app --reload
```

**Tab 2 — Generation worker** (LLM calls, I/O-bound)
```bash
.venv/bin/celery -A worker.celery_app worker -Q generation -c 4 -l info
```

**Tab 3 — Render worker** (ffmpeg, CPU-bound — keep concurrency at 1)
```bash
.venv/bin/celery -A worker.celery_app worker -Q rendering --concurrency=1 -l info
```

**Tab 4 — Beat scheduler** (stuck-job reaper, every 60 s)
```bash
.venv/bin/celery -A worker.celery_app beat -l info
```

**Tab 5 — LLM** (skip if using NVIDIA NIM)
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
4. Choose a **target length**: 30s, 45s, 60s, 75s, or 90s.
5. Choose a **generation path**: `Auto-detect`, `I wrote a script` (fast structured path, ~90 s), or `Generate from topic` (full LLM, 2–5 min).
6. Click **Generate guide**.

The input is first scored for engagement potential (0–100) and enriched by an LLM if it scores below 60 — skipped automatically when you supply a structured script, since enrichment would drift off topic.

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

**Kokoro** (`TTS_PROVIDER=kokoro`) is an alternative local neural TTS with higher quality, but
requires **Python < 3.13**:

```bash
.venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/pip install -e ".[tts]"
```

---

## Optional: Word-level captions with Whisper

```bash
.venv/bin/pip install -e ".[captions]"
# ffmpeg must be on PATH
```

When installed, the compositor transcribes each beat's audio and drives on-screen text timing from
word-level timestamps instead of proportional word-count estimates. Without it, timing falls back to
proportional automatically — no configuration needed either way.

---

## Configuration reference

All values are read from `.env` (or environment variables):

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://reelmaker:reelmaker@localhost:5432/reelmaker` | Postgres connection string |
| `REDIS_URL` | `redis://localhost:6379/0` | Celery broker and result backend |
| `LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible LLM endpoint (Ollama) |
| `LLM_MODEL` | `qwen3:14b` | Model name passed to the LLM endpoint |
| `LLM_ENRICHMENT_MODEL` | `qwen3:14b` | Local model for enrichment + quality judge |
| `NVIDIA_API_KEY` | *(empty)* | When set, enrichment + judge calls route to NVIDIA NIM |
| `NVIDIA_ENRICHMENT_MODEL` | `nvidia/nemotron-3-super-120b-a12b` | Hosted enrichment/judge model |
| `NVIDIA_GENERATION_MODEL` | `nvidia/nemotron-3-super-120b-a12b` | Hosted generation model |
| `USE_NVIDIA_FOR_GENERATION` | `false` | Route main guide generation to NVIDIA NIM too |
| `TTS_PROVIDER` | `edge` | `edge` (needs `edge-tts`), `kokoro` (Python < 3.13), or `silent` |
| `PEXELS_API_KEY` | *(empty)* | Required for stock footage; falls back to black frames |
| `HUGGINGFACE_API_KEY` | *(empty)* | Generates footage/images when Pexels and Wikipedia find nothing |
| `PIXABAY_API_KEY` | *(empty)* | Alternative footage source (not yet wired in) |
| `CREDENTIALS_KEY` | *(empty)* | Fernet key encrypting `credentials.token_blob`; plaintext with a warning when blank |
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
