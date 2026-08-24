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
  models.py         mandates, failure_events, retry_attempts, classifications,
                     retry_plans, planned_attempts, audit_log
  simulator.py       Day-2 failure simulator (P1-P12 taxonomy injector)
  classifier.py       Day-3 rule filter + Groq LLM path (P3/P12 only)
  constraints.py      Day-4 hard veto layer (NPCI/RBI-cited + operational-assumption rules)
  planner.py          Day-5 deterministic cost-based retry planner (no LLM)
  executor.py          Day-6 executes plans against Razorpay (idempotent, concurrency-safe, backoff)
  routers/          API route modules (added as built)
docs/
  failure_taxonomy.md   payment + system failure taxonomy (spec)
  build_schedule.md     day-by-day build plan
  constraints.md         constraint-store rule sources (cited vs. operational assumption)
  architecture.md       architecture explanation (written during build)
  eval_audit.md          rigor/bug-tracking log, written as stress-testing proceeds
tests/
scripts/
  seed_test_mandate.py  one-off script to confirm Razorpay test-mode connectivity
  seed_mandates.py       seeds synthetic active mandates across the three rails
  inject_failures.py      batch-injects failure_events via the simulator
  classify_batch.py       runs the classifier over unclassified failure_events
```

## Setup

1. `python -m venv venv && source venv/bin/activate`
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in your Razorpay test-mode key ID and secret.
4. `uvicorn app.main:app --reload`

## Executor: real Razorpay calls vs. stub

`app/executor.py` decides real-vs-stub per mandate, not globally: if
`mandate.razorpay_token` is set (a completed, human-authorized Razorpay
recurring-payment token), it calls the real test-mode API; otherwise it
calls a stub shaped like a real response (`{"stub": True, ...}`, always
visible in the `retry_attempt_executed` audit_log entry's detail).

**As of the current build, `razorpay_token` is `None` on every mandate in
this project's seed data.** Day 2 generated three real Razorpay
registration links (via the `auth_links` API) intended for exactly this
purpose, but completing one requires a human to open the link and approve
via netbanking/UPI app/debit card -- there's no headless equivalent (see
`docs/eval_audit.md`, 2026-08-23). Checked directly against the Razorpay
API before writing this: all three invoices still read `status: "issued"`,
`payment_id: None` -- nobody has clicked through any of them yet. So today,
every execution in a demo run goes through the stub path. If you want the
demo video to show one real API call, open one of the Day-2 registration
links yourself and let a follow-up script attach the resulting token to
that mandate; until then, "real vs. stub" is a structurally-correct but
currently-unexercised code path, and the demo script should say so rather
than imply otherwise.

## Status

Day 6 of 12. Simulator, classifier, constraint store, planner, and executor
are built and tested (79 tests). See `docs/build_schedule.md` for the
build plan and `docs/failure_taxonomy.md` for the classification spec this
system is built against. Still to come: fallback agent, end-to-end
integration, eval harness, stress-test day.
