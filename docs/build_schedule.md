# Build schedule — Mandate Retry Orchestrator

Deadline: Sep 5, 2026. 12 build days (Aug 24 – Sep 4) plus Sep 5 as a submission-only buffer day — no new code that day, only final checks.

## Critical path vs. nice-to-have

If you fall behind, cut in this order (bottom first): simulator, classifier (rules + LLM), constraint store, and planner are **not cuttable** — that's the whole thesis of the project. The executor is not cuttable either, since it's what makes it "a working prototype" rather than a design doc. The fallback agent, the dashboard, and multi-seed statistical rigor are real but secondary — a working three-stage pipeline (classify → plan → execute) with two or three well-documented Part 2 stress tests beats a five-stage pipeline that's half-broken.

---

### Day 1 — Aug 24: Scaffold
- Repo structure, FastAPI skeleton, SQLite schema: `mandates`, `failure_events`, `retry_attempts`, `audit_log`.
- Razorpay test-mode account + API keys; confirm you can create a test mandate and trigger a debit attempt end to end, even if it always succeeds.
- Commit early — you want commit history showing incremental, legible progress, not one giant dump on day 10.

### Day 2 — Aug 25: Failure simulator
- Implement injection for every category in the payment taxonomy (P1–P12), each carrying a ground-truth recoverability label.
- Failure event capture: whatever fails gets written to `failure_events` in a consistent schema the classifier will consume.

### Day 3 — Aug 26: Classifier
- Rule filter for unambiguous failure codes (most of P1–P11 should resolve without an LLM call).
- Wire the LLM (Claude) for the genuinely ambiguous case (P12) with a strict structured-output contract.
- Build the fallback for malformed LLM output now, not later (S1) — treat any parse failure as "ambiguous, single cautious retry."

### Day 4 — Aug 27: Constraint store
- Hardcode NPCI/bank cooldown windows, mandate expiry checks, debit-limit rules as an independent module.
- Design it as a hard veto interface the planner must call, not a suggestion the planner can ignore (this sets up S6).

### Day 5 — Aug 28: Retry planner
- Cost-based search over candidate retry sequences (attempt count, spacing, channel) — this is your GOAP-adjacent core, keep it deterministic.
- Constraint store integration as a post-planning veto check (S6).
- "Do not retry, escalate" must be a legitimate planner output, not just retry sequences (S7).

### Day 6 — Aug 29: Executor
- Actually run retries against Razorpay test-mode API on the planner's schedule.
- Idempotency key per (mandate, failure event) to kill duplicate scheduling (S3).
- Row-level lock or unique constraint before executing, to prevent concurrent double-execution (S4).
- Exponential backoff with jitter for Razorpay API-layer failures, kept distinct from mandate-level retry logic (S5).

### Day 7 — Aug 30: Fallback agent + audit logging
- Template-constrained generation for the customer/merchant-facing fallback message, with a validation pass before anything is sent (S8).
- Audit log write path with a durability check — never let a pipeline step silently proceed without a logged record (S9).

### Day 8 — Aug 31: End-to-end integration
- Run the full pipeline across all P1–P12 scenarios.
- Stand up the first pass of the eval harness: classifier accuracy per category, retry success rate, simulated recovered revenue.

### Day 9 — Sep 1: Stress-test day
- Deliberately trigger S1–S9 one at a time. Document what actually breaks (something will), fix it, log the before/after.
- This log is your `eval_audit.md`-equivalent for this project — it's the single most important artifact for the "Failure Recovery" criterion, so don't skip writing it up as you go.

### Day 10 — Sep 2: Rigor + visualization
- Multi-seed evaluation runs if time allows (this is the differentiator, not the requirement — see cut order above).
- Minimal dashboard or API view showing a live classify → plan → execute → outcome trace, for the demo video.

### Day 11 — Sep 3: Repo polish
- README with problem statement, architecture diagram, setup instructions.
- Architecture explanation doc (the written artifact Razorpay explicitly asks for) — this can lean heavily on the LLM-boundary explanation and the two-part failure taxonomy.
- Clean up commit history and code comments; this is scored under Build Quality.

### Day 12 — Sep 4: Demo video + pitch
- Script, record, and edit the 5-minute demo.
- Dry-run the pitch as if it's the panel interview — be ready to explain, unprompted, why the planner isn't an LLM and why the classifier mostly isn't either.

### Sep 5 — Submission day
- No new code. Final checklist, repo visibility check, submit.

---

## What to have ready to say, unprompted, in the panel interview
- Why the LLM is only in two places, and how much of the pipeline runs without it.
- One S-series failure you found that you didn't expect going in, and what you changed because of it.
- The actual recovered-revenue number from your simulation, with the caveat that it's simulated, not from real merchant data.
