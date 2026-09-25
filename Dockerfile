# One image for every process this app runs — API, both Celery worker roles, and beat.
# Which one a container becomes is decided by `command:` in docker-compose.yml, not by
# separate Dockerfiles: they share the exact same dependency set (moviepy/ffmpeg,
# edge-tts, psycopg2, ...), so a second image would just double the build and the
# chance for the two to drift apart.
FROM python:3.12-slim

# ffmpeg: required by MoviePy/the compositor (engine/render/compositor.py) and, if the
# optional [captions] extra is installed, by Whisper transcription too — not a Python
# dependency, must come from the OS package manager. Matches .github/workflows/ci.yml's
# own ffmpeg install exactly, so what passes CI is what this image runs.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency manifests first so `pip install` is cached across rebuilds that only
# touch application code — the common case during iteration.
COPY pyproject.toml ./
COPY api ./api
COPY worker ./worker
COPY engine ./engine

# Non-editable install (this image ships a fixed copy of the code, not a dev checkout);
# edge-tts is a runtime requirement whenever TTS_PROVIDER=edge (the default — see
# api/config.py) but isn't in pyproject.toml's own dependency list, matching this
# repo's documented local setup (`pip install edge-tts` as a separate step, CLAUDE.md's
# Commands section) exactly rather than inventing a second install path just for Docker.
RUN pip install --no-cache-dir . && pip install --no-cache-dir edge-tts

COPY migrations ./migrations
COPY alembic.ini ./
COPY ui ./ui

# Runs as an unprivileged user — this image never needs root once dependencies are
# installed. ASSET_STORE_DIR/VIDEO_STORE_DIR/MUSIC_LIBRARY_DIR default to ./data/... in
# api/config.py; docker-compose.yml bind-mounts ./data there (see that file), so this
# chown only needs to cover the app directory itself, not a data volume it doesn't own.
RUN useradd --create-home --uid 1000 reelmaker && chown -R reelmaker:reelmaker /app
USER reelmaker

EXPOSE 8000

# Default to the API process; docker-compose.yml overrides `command:` for the
# generation worker, rendering worker, and beat scheduler roles. Migrations are
# deliberately NOT run here — see docker-compose.yml's comment on why that has to be a
# separate, explicit, single-run step rather than baked into every container's startup.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
