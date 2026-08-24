"""FastAPI entrypoint. Fleshed out during Day 1 build - see docs/build_schedule.md."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.db import init_db
from app.docs_glossary import ESCALATION_TYPE_GLOSSARY, parse_demo_scenarios, parse_taxonomy_table
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


@app.get("/help")
def trace_viewer_help():
    """Legend + domain glossary for the trace viewer -- see
    static/help.html. Glossary content is pulled from /api/glossary at
    render time, not hand-duplicated."""
    return FileResponse(STATIC_DIR / "help.html")


@app.get("/api/glossary")
def glossary() -> dict:
    """Read-only, doc-derived content for the trace viewer's /help page:
    the P1-P12 taxonomy table parsed from docs/failure_taxonomy.md, and
    the four escalation types. Parsing at request time means this can't
    silently drift from the taxonomy doc as it changes."""
    return {
        "taxonomy": parse_taxonomy_table(),
        "escalation_types": ESCALATION_TYPE_GLOSSARY,
    }


@app.get("/api/demo-scenarios")
def demo_scenarios() -> list[dict]:
    """Read-only: the curated mandate table from docs/demo_mandates.md,
    parsed at request time so the trace viewer's pinned sidebar section
    can't drift from the doc Day 12's demo script references."""
    return parse_demo_scenarios()
