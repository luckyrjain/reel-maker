import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.routers import reels, jobs, cuts, credentials, insights
from engine.generation.llm import validate_configured_models
from engine.render.compositor import CURATED_TEXT_COLORS
from engine.render.tts import CURATED_EDGE_VOICES

_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Best-effort, synchronous, ~5s max — see validate_configured_models()'s docstring
    # for why a local-Ollama warning here is routinely a false positive (this repo's own
    # documented dev setup starts the API before `ollama serve`) but an NVIDIA NIM one
    # isn't. Never blocks startup on a failure — this is visibility, not a hard gate;
    # the app already has plenty of graceful-degradation precedent (asset_sourcer's
    # fallback chain, SilentProvider) and a dead LLM endpoint shouldn't take down a
    # server that's otherwise fine for browsing/reviewing existing reels.
    try:
        for warning in validate_configured_models():
            _log.warning("Startup model check: %s", warning)
    except Exception:
        # validate_configured_models() is documented as never-raising, but this is
        # a startup check — it must never be able to take the app down even if that
        # promise is broken again in the future.
        _log.exception("Startup model check failed unexpectedly — continuing startup")
    yield


app = FastAPI(title="Reel Maker", lifespan=lifespan)
templates = Jinja2Templates(directory="ui/templates")
app.mount("/static", StaticFiles(directory="ui/static"), name="static")

app.include_router(reels.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(cuts.router, prefix="/api")
app.include_router(credentials.router, prefix="/api")
app.include_router(insights.router, prefix="/api")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request, "index.html",
        {"tts_voices": CURATED_EDGE_VOICES, "text_colors": CURATED_TEXT_COLORS},
    )
