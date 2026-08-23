# Failure taxonomy — Mandate Retry Orchestrator

This document is the spec the classifier and planner are built against. It has two parts:

1. **Payment/mandate failure taxonomy** — categories the classifier assigns to a failed debit.
2. **System/orchestration failure taxonomy** — failure modes in the orchestrator itself, and the required fallback for each. This second part is what actually earns "Failure Recovery" points — it's evidence the system was stress-tested against itself, not just against payment failure codes.

Every category below needs: a simulated trigger (for the failure simulator), a ground-truth recoverability label (for evaluating classifier accuracy), and a defined system behavior.

---

## Part 1 — Payment / mandate failure taxonomy

| ID | Rail | Failure | Nature | Typical recovery window | Retry-worthy | Strategy implication |
|----|------|---------|--------|--------------------------|---------------|------------------------|
| P1 | UPI Autopay / e-NACH | Insufficient balance at debit time | Transient | Hours to days (often clusters near salary cycle) | Yes | Delay retry, bias toward late-month / early-next-month windows |
| P2 | Any | Issuing bank server / NPCI timeout | Transient (technical) | Minutes to hours | Yes, aggressively | Short-delay retry, 1–2 quick attempts before backing off |
| P3 | Any | Risk / fraud hold by issuing bank | Ambiguous | Unknown, bank-side | Cautious yes | Single delayed retry only; repeated attempts risk mandate flagging |
| P4 | Any | Mandate expired | Hard | N/A — same mandate unrecoverable | No | Do not retry; trigger re-authorization fallback |
| P5 | Any | Mandate revoked / paused by customer | Hard | N/A | No | Do not retry; requires fresh consent |
| P6 | UPI Autopay | Mandate debit-limit breached (e.g. ₹15,000 cap or configured limit) | Hard for this cycle | Resets next cycle | No (this cycle) | Fallback to split payment or wait for next cycle |
| P7 | UPI Autopay | Pre-debit notification not acknowledged | Procedural | Depends on notification retry window | Conditional | Retry only within valid notification window, else treat as hard fail |
| P8 | Card e-mandate | Card expired / blocked | Hard | N/A | No | Do not retry; requires re-tokenization flow |
| P9 | Card e-mandate | Issuing bank downtime for e-mandate execution | Transient | Hours | Yes | Short-delay retry |
| P10 | e-NACH | Physical clearing cycle delay | Semi-transient | 1–3 business days (clearing-cycle bound) | Yes, but slow | Retry timing must respect clearing calendar, not wall-clock delay |
| P11 | Any | Customer account frozen / KYC issue | Hard-ish | Unknown, likely long | No (or single distant retry) | Escalate to merchant, don't burn retry budget |
| P12 | Any | Ambiguous / unclassified decline code | Ambiguous | Unknown | Classifier escalates to LLM | Only path where the LLM classifier is actually invoked |

**Note on P3, P6, P7, P11**: these are the categories worth emphasizing in your writeup — they're where naive "just retry N times on a fixed schedule" systems actively cause harm (bank flagging, wasted NPCI retry budget, customer annoyance), which is exactly the gap this project is closing.

---

## Part 2 — System / orchestration failure taxonomy

These are failure modes of the orchestrator itself. Each needs a deliberate, engineered fallback — this is the section to walk through in your demo video and architecture explanation, since it's the part most competing submissions won't have.

| ID | Failure mode | Trigger to simulate | Required system behavior |
|----|--------------|----------------------|----------------------------|
| S1 | LLM classifier returns malformed / unparseable output | Inject malformed JSON from the LLM call | Fall back to a conservative default classification (treat as ambiguous, single cautious retry only) — never crash the pipeline on a bad LLM response |
| S2 | LLM classifier is confidently wrong | Feed a known P4 (hard fail) case and check if classifier mislabels it as retryable | Cap maximum retry attempts per mandate regardless of classifier output — a hard ceiling the LLM cannot override |
| S3 | Retry storm / duplicate retry scheduling | Trigger the same failure event twice (e.g. webhook delivered twice) | Idempotency key per (mandate, failure event) — second trigger is a no-op, logged not executed |
| S4 | Double-charge race condition | Fire two retry executions concurrently for the same mandate | Row-level lock or unique constraint on (mandate_id, attempt_window) before executing a retry |
| S5 | Razorpay test API unavailable or rate-limited | Simulate a 5xx or 429 from the API layer | Exponential backoff with jitter at the executor level, distinct from the payment-domain retry logic — don't conflate API retry with mandate retry |
| S6 | Constraint store and planner disagree (planner proposes a retry that violates an NPCI/bank cooldown) | Feed a plan that violates a hard constraint | Constraint store is a hard veto layer checked *after* planning, before execution — plan is never trusted blindly |
| S7 | Retry planner produces a low-value/negative-expected-value plan | Force a scenario where all retry options have negative expected value | Planner must be able to output "do not retry, escalate to fallback agent" as a valid action, not just retry sequences |
| S8 | Fallback agent generates an inappropriate customer-facing message | Feed edge-case context (e.g. customer already escalated, or mandate revoked) | Template-constrained generation with a validation pass — reject and use a safe default template if output fails validation |
| S9 | Audit log write fails mid-pipeline | Simulate a DB write failure during logging | Pipeline must not silently continue without a durable log entry — retry the write or fail loudly, never lose the audit trail silently |

**Note on S3 and S4**: these two are worth calling out explicitly in your pitch — a retry system that can double-charge a customer is a worse outcome than no retry system at all, and demonstrating you found and closed this gap is a very concrete "Failure Recovery" story.

---

## How this maps to the build

- The **failure simulator** needs to be able to inject every row in Part 1 with a ground-truth label, so you can report classifier accuracy per category (and per rail).
- The **eval/audit harness** should separately track: (a) classification accuracy against Part 1 ground truth, and (b) whether each Part 2 fallback actually fires correctly when its trigger condition is simulated — treat S1–S9 as a checklist you can literally show passing/failing in your repo's test output.
- In the demo video and pitch, lead with 2–3 concrete Part 2 stories (e.g. S3, S4, S7) — these are more memorable and more clearly "found a real failure mode" than a long list of payment decline codes.
