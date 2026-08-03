from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.routers import reels, jobs, cuts, credentials

app = FastAPI(title="Reel Maker")
templates = Jinja2Templates(directory="ui/templates")
app.mount("/static", StaticFiles(directory="ui/static"), name="static")

app.include_router(reels.router, prefix="/api")
app.include_router(jobs.router, prefix="/api")
app.include_router(cuts.router, prefix="/api")
app.include_router(credentials.router, prefix="/api")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")
