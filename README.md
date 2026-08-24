# Mandate Retry Orchestrator

A system that classifies failed recurring-payment mandates (UPI Autopay, e-NACH, card e-mandates), plans a cost-based retry sequence under real bank/NPCI constraints, executes it against Razorpay's test-mode APIs, and falls back to a template-constrained human-facing message when retrying isn't the right call.

Built for the Razorpay AI Buildathon 2026 — AI Growth & Agentic Commerce track.

## Problem

Recurring-payment mandates fail for many reasons — some transient (bank timeout, momentary insufficient balance), some hard (expired mandate, revoked consent), some ambiguous (risk holds). Most merchants either don't retry, retry on a fixed blind schedule, or retry so aggressively that banks flag the mandate for abuse. This project treats retry as a planning problem: classify the failure, check what's actually allowed under NPCI/RBI constraints and mandate state, then choose a retry sequence that maximizes expected recovered revenue — or explicitly decide not to retry at all, which is a first-class outcome, not a fallback.

## Architecture

**See [`docs/architecture.md`](docs/architecture.md) for the full writeup** — pipeline design, where the LLM is and isn't used and why (with the actual measured numbers), the two hardest-earned findings from stress-testing, a structural tension that was deliberately left unfixed, why recovered revenue is reported as a range, and the current S1–S9 system-failure-mode checklist.

Short version: `simulator → classifier → constraints → planner → executor → fallback (only on escalate) → audit_log`. Groq is used in exactly two places (classifier's two genuinely ambiguous categories; fallback message phrasing) — everything else is deterministic code, including the retry planner, which has no LLM anywhere in it.

## Repo layout

```
app/
  main.py           FastAPI entrypoint (Day 1 scope — read-only /mandates API,
                     proves the DB layer works; the real logic lives in the
                     pipeline modules below, not the API layer)
  db.py             SQLAlchemy engine/session setup, pydantic-settings config
  models.py         Full schema: mandates, failure_events, classifications,
                     retry_plans, planned_attempts, retry_attempts,
                     fallback_messages, audit_log
  simulator.py      Failure simulator: injects P1-P12 taxonomy failures with
                     ground-truth recoverability labels
  classifier.py     Rule filter (most categories) + Groq LLM path (P3/P12 only)
  constraints.py    Hard veto layer: NPCI/RBI-cited rules + labeled
                     operational assumptions (see docs/constraints.md)
  planner.py        Deterministic, cost-based retry sequence search. No LLM.
  executor.py       Idempotent, concurrency-safe execution against Razorpay
                     (real or stub), backoff, stuck-row reclaim
  fallback.py       Template-constrained fallback message generation
                     (2nd and last Groq use); template selection is code,
                     not the LLM
  eval.py           Classifier accuracy, escalation-type distribution,
                     simulated recovered revenue, S1-S9 checklist
  routers/
    mandates.py     GET /mandates, GET /mandates/{id}
docs/
  failure_taxonomy.md   Payment (P1-P12) + system (S1-S9) failure taxonomy spec,
                         with a dated revision note on the P3 ground-truth fix
  build_schedule.md     Original day-by-day build plan
  constraints.md         Constraint-store rule sources (cited vs. assumption)
  architecture.md        The real architecture writeup — start here
  eval_audit.md          Chronological findings/fixes log from stress-testing
  multi_seed_eval.md     5-seed variance analysis on the headline eval numbers
tests/              One test file per app/ module, 117 tests total
scripts/
  seed_test_mandate.py   One-off Razorpay test-mode connectivity check
  seed_mandates.py       Seeds synthetic active mandates across the 3 rails
  inject_failures.py     Batch-injects failure_events via the simulator
  classify_batch.py      Runs the classifier over unclassified failure_events
  generate_fallbacks.py  Runs the fallback agent over escalated plans
  run_pipeline.py        Full end-to-end integration run, no manual steps
                          between stages; prints a per-mandate trace
  eval_report.py         Renders app/eval.py as a markdown report
  multi_seed_eval.py     Re-runs the pipeline across N seeds, reports variance
```

## Setup

Tested from a clean clone (see "Verifying setup from clean," below):

1. `git clone <repo-url> && cd mandate-retry-orchestrator`
2. `python -m venv venv`
   - Windows: `venv\Scripts\activate`
   - macOS/Linux: `source venv/bin/activate`
3. `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and fill in your Razorpay test-mode key ID/secret and your Groq API key.
5. Verify the install: `pytest tests/ -q` — should show `117 passed`.
6. See something real happen: `python scripts/seed_mandates.py --count 20 && python scripts/inject_failures.py --count 60 && python scripts/classify_batch.py` — seeds mandates, injects failures across the taxonomy, classifies them against the real Groq API, and prints the rule-vs-LLM split.
7. Or run the whole pipeline at once: `python scripts/run_pipeline.py` — simulate → classify → plan → execute → fallback → audit, end to end, with a full trace for one mandate printed at the end.
8. `uvicorn app.main:app --reload` starts the (minimal, read-only) API layer if you want to hit `GET /mandates` directly.

### Verifying setup from clean

The steps above were run against an actual fresh `git clone` into a separate directory (not just described from memory of writing them) — new venv, `pip install -r requirements.txt` from empty, real `.env` filled with working test-mode credentials, `pytest` (117 passed), `scripts/seed_mandates.py`/`inject_failures.py`/`classify_batch.py`, `scripts/run_pipeline.py`, and `uvicorn app.main:app` all run to completion. See the Day 11 commit for the exact verification transcript.

**Groq free-tier daily token quota**: a day of repeated real-API testing (classifier + fallback agent calls, across dozens of batch runs) is enough to hit Groq's free-tier 200,000-tokens-per-day limit. When that happens you'll see `429 rate_limit_exceeded` errors in `audit_log` — which is actually a clean real-world demonstration of the S1/S8 fallback mechanisms working (both fall back to a safe default and log it, rather than crashing), not a bug. If you're reproducing the eval numbers, budget for this or spread runs across a day/account.

## Executor: real Razorpay calls vs. stub

`app/executor.py` decides real-vs-stub per mandate, not globally: if `mandate.razorpay_token` is set (a completed, human-authorized Razorpay recurring-payment token), it calls the real test-mode API; otherwise it calls a stub shaped like a real response (`{"stub": True, ...}`, always visible in the `retry_attempt_executed` audit_log entry's detail).

**As of the last verification (Day 11), `razorpay_token` is still `None` on every mandate — no registration link has been authorized on this Razorpay test account.** Checked directly against the API: all known invoices/orders remain unauthorized, and the account has zero payments and zero customer tokens across all three test customers. Registration links were generated (`scripts/seed_mandates.py --with-real-auth-link`) but completing one requires a human to open the link and approve via netbanking/UPI app/debit card — there's no headless equivalent (`docs/eval_audit.md`, 2026-08-23 entry). Every execution in a run today goes through the stub path; "real vs. stub" is a structurally correct, tested code path, currently unexercised against a real authorized mandate.

## Status

Feature-complete through the full pipeline (simulator → classifier → constraints → planner → executor → fallback → audit), plus an eval harness, stress-testing, and multi-seed evaluation. 117 tests, all passing. See `docs/architecture.md` for the real writeup and `docs/eval_audit.md` for the chronological findings/fixes log.

## License

MIT — see [`LICENSE`](LICENSE).
