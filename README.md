# Mandate Retry Orchestrator

An agent that classifies failed recurring-payment mandates (UPI Autopay, e-NACH, card e-mandates), plans a cost-based retry sequence under real bank/NPCI constraints, executes it via Razorpay's test-mode APIs, and falls back gracefully when retries aren't appropriate.

Built for the Razorpay AI Buildathon 2026 — AI Growth & Agentic Commerce track.

## Problem

Recurring-payment mandates fail for many reasons — some transient (bank timeout, momentary insufficient balance), some hard (expired mandate, revoked consent), some ambiguous (risk holds). Most merchants either don't retry, retry on a fixed blind schedule, or retry so aggressively that banks flag the mandate for abuse. This project treats retry as a planning problem: classify the failure, then choose a retry sequence that maximizes expected recovered revenue under real constraints (NPCI retry-window limits, bank cooldowns, mandate expiry, diminishing success probability per attempt).

## Architecture

See `docs/architecture.md` (in progress) for the full writeup. Pipeline: failure simulator → classifier (rule filter + LLM only for ambiguous cases) → retry planner (deterministic, cost-based sequencing, constraint-checked) → executor (Razorpay test API) → fallback agent (LLM-drafted recovery message when retries are exhausted) → audit log.

## Repo layout

```
app/
  main.py           FastAPI entrypoint
  db.py             SQLAlchemy engine/session setup
  models.py         mandates, failure_events, retry_attempts, audit_log
  routers/          API route modules (added as built)
docs/
  failure_taxonomy.md   payment + system failure taxonomy (spec)
  build_schedule.md     day-by-day build plan
  architecture.md       architecture explanation (written during build)
  eval_audit.md          rigor/bug-tracking log, written as stress-testing proceeds
tests/
scripts/
  seed_test_mandate.py  one-off script to confirm Razorpay test-mode connectivity
```

## Setup

1. `python -m venv venv && source venv/bin/activate`
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in your Razorpay test-mode key ID and secret.
4. `uvicorn app.main:app --reload`

## Status

Early scaffold. See `docs/build_schedule.md` for the current build plan and `docs/failure_taxonomy.md` for the classification spec this system is built against.
