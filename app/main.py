"""FastAPI entrypoint. Fleshed out during Day 1 build - see docs/build_schedule.md."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import init_db
from app.routers import mandates


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Mandate Retry Orchestrator", lifespan=lifespan)
app.include_router(mandates.router)


@app.get("/health")
def health():
    return {"status": "ok"}
