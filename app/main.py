"""FastAPI entrypoint. Fleshed out during Day 1 build - see docs/build_schedule.md."""
from fastapi import FastAPI

app = FastAPI(title="Mandate Retry Orchestrator")


@app.get("/health")
def health():
    return {"status": "ok"}
