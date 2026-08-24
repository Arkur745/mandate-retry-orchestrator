"""FastAPI entrypoint. Fleshed out during Day 1 build - see docs/build_schedule.md."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.db import init_db
from app.routers import mandates

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Mandate Retry Orchestrator", lifespan=lifespan)
app.include_router(mandates.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def trace_viewer():
    """Read-only trace viewer -- see static/trace_viewer.html. Plain
    HTML/CSS/JS, no build step, no framework; calls GET /mandates and
    GET /mandates/{id}/trace directly."""
    return FileResponse(STATIC_DIR / "trace_viewer.html")
