# Architecture — Mandate Retry Orchestrator

## The problem, in one paragraph

Recurring-payment mandates (UPI Autopay, e-NACH, card e-mandates) fail for reasons that span a wide range of urgency and recoverability — a momentary bank timeout has nothing in common with a revoked mandate, yet naive retry systems treat them identically: retry N times on a fixed schedule regardless of cause. That either wastes retry budget on hopeless cases or, worse, retries aggressively enough to get a mandate flagged for abuse by the bank/NPCI. This project treats retry as a planning problem under real constraints, not a blind loop: classify why the debit failed, check what's actually allowed given NPCI/RBI rules and mandate state, plan a cost-based retry sequence only where the numbers justify it, execute it safely, and fall back to a template-constrained human-facing message when retry isn't the right call.

## Pipeline

```
simulator → classifier → constraints ↴
                             ↓         (planner calls constraints per candidate)
                          planner → executor → audit_log
                             ↓
                         fallback (only on "escalate")
```

Seven modules, each Day-numbered in the build history (`docs/build_schedule.md`), each with its own test file:

| Module | File | Job |
|---|---|---|
| Simulator | `app/simulator.py` | Injects synthetic `failure_events` across the P1–P12 taxonomy, each with a ground-truth recoverability label and realistic bank/NPCI-shaped decline text |
| Classifier | `app/classifier.py` | Rule lookup for unambiguous categories; Groq LLM only for the two genuinely ambiguous ones (P3, P12) |
| Constraint store | `app/constraints.py` | Hard veto layer — NPCI/RBI-cited rules plus explicitly labeled operational assumptions; never proposes, only allows/blocks |
| Planner | `app/planner.py` | Deterministic, cost-based search over candidate retry sequences; no LLM anywhere in this module |
| Executor | `app/executor.py` | Idempotent, concurrency-safe execution against Razorpay (real or stub), with backoff and a stuck-row reclaim mechanism |
| Fallback agent | `app/fallback.py` | Template-constrained message generation for `escalate` decisions; the second and last place an LLM is used |
| Eval harness | `app/eval.py` | Classifier accuracy, escalation-type distribution, simulated recovered revenue, S1–S9 test-coverage checklist — all computed from the DB, not hand-tracked |

Every stage writes to `audit_log`; `scripts/run_pipeline.py` drives all of them end to end for a batch and prints a full per-mandate trace, confirmed to require no manual intervention between stages.

## Where the LLM is, and isn't, and why

Two places only, both Groq, and both by deliberate exclusion rather than default inclusion:

1. **Classifier, P3 and P12 only.** Every other category (P1, P2, P4–P11) resolves via a direct dict lookup against the taxonomy's own "Retry-worthy" column — including P7, whose "Conditional" answer looks like it needs judgment but is actually a deterministic text check (is the notification-ack window still open), and P6/P8/P11, whose answers are simply fixed. P3 (risk/fraud hold) and P12 (unclassified decline) are the two categories the taxonomy document itself flags as genuinely ambiguous — those get an LLM call with strict JSON output, validated before use (see S1 below).
2. **Fallback agent, template *filling* only, never template *selection*.** Given an `escalate` decision, which message template applies (`retry_exhausted_nudge`, `reauth_needed`, `rail_switch_recommended`, `merchant_escalation`) is decided by code (`determine_escalation_type`), not the LLM — sending a customer the wrong *category* of message is high-stakes and the mapping is already fully known from data the system has, so there's no genuine ambiguity to hand to an LLM. The LLM's job is narrower: given the already-selected template, write the natural-language phrasing. Facts like the amount are injected by code, never generated, removing most of the hallucination surface outright.

**The numbers**, measured against the real Groq API (not mocked), most recent first:

- **Classifier accuracy: 99.69% mean across 5 independent seeds** (stdev 0.70%, range [98.44%, 100%] — `docs/multi_seed_eval.md`), post the Day 10 P3 ground-truth correction described below. Pre-correction, the same measurement was 93.0% aggregate (Day 8) — the entire gap was P3 specifically (15.8%), not a general classifier weakness; every other category was already at or near 100%.
- **Rule-vs-LLM split: ~84–85% resolved by rule, ~15–16% routed to the LLM** (Day 3 real batch: 84.5%/15.5%; Day 8 batch: 85.3%/14.7% — consistent). Most of what looks like "the classifier's job" is actually zero-LLM-call dictionary lookup; the LLM is load-bearing for a genuinely narrow slice.

## Two hard-earned findings

### 1. The time-of-day dead zone (found Day 8, root-caused and fixed Day 9)

A real 250-event pipeline run happened to start at 17:05 IST and showed 100% of P2 (bank/NPCI timeout) retries escalating — on `upi_autopay`, which has none of the known e-NACH/card-emandate spacing-floor issue. Root cause: `fast_technical`'s two fastest candidate offsets (0.5h, 1h) both landed inside UPI's 17:00–21:30 IST peak window, and since a candidate sequence only uses consecutive slots starting from the first, the *entire* search space was empty — not degraded, empty — purely as a function of what time of day the failure happened to occur.

A deliberate full sweep (every real category/rail combination, every minute of a 24h cycle) on Day 9 found this was **not isolated to `fast_technical`**: `delayed_funds` (P1), `cautious_single` (P3, P12), and `notification_window` (P7) were all similarly dead for 27–31% of each day on `upi_autopay`, because their offsets are either sub-day or exact multiples of 24h — the latter land at the *identical* clock time as the original failure, so if that time-of-day is bad, every offset in the profile is equally bad at once.

**Fix:** `app.constraints.next_non_peak_window_start(dt_utc, rail)` — a pure utility, not a change to `check_retry`'s veto behavior (Day 4's own design comment had already called for this exact split: *"Re-proposing a corrected timestamp is the planner's job, not this module's."*). The planner now shifts a candidate forward to the next valid window instead of proposing one that's certain to be vetoed. Verified with a full re-sweep: all five partial dead zones are now clean; the three *genuinely structural* always-dead combinations (below) are byte-for-byte unchanged.

### 2. The P3 ground-truth correction (investigated Day 9, decided and applied Day 10)

The classifier's 15.8% agreement on P3 looked, at first glance, like a classifier problem. Reading the actual `reasoning` field for every disagreement (not just the accuracy number) showed it wasn't noise: the disagreement was perfectly correlated with which of the simulator's three P3 decline-text variants was used. The two variants that explicitly state "Suspected Fraud" (one citing RC=59, a real card-network fraud-decline code) were judged non-recoverable *every single time*, consistently reasoned as needing bank-side resolution — standard real-world payments practice. The one variant without fraud language, describing a customer-confirmation path instead, was judged recoverable every time.

The taxonomy's original ground truth labeled all three variants uniformly recoverable. The investigation's conclusion — that the classifier was differentiating on real textual content, and had a defensible claim to be catching a distinction the taxonomy had collapsed — was reported with evidence, not acted on unilaterally; the correction was applied only after that review. `app/simulator.py`'s P3 variants now carry per-variant ground truth; `docs/failure_taxonomy.md` has a dated revision note. No classifier or prompt code was touched — the classifier's behavior was the evidence, not the target of a fix.

## A structural tension left as-is: P9 and the rail-spacing floor

`card_emandate`'s and `e_nach`'s Day-4 "minimum 24h between attempts" rule (an explicitly labeled *operational assumption*, not a cited regulation) is measured from the original failure, not just between retries — so P9 (issuing-bank downtime, card e-mandate only) and P2 on those same rails can never produce a valid retry plan: `fast_technical`'s fastest offset (0.5h) can never satisfy a 24h floor. This is different in kind from the dead zone above — it's not a function of *when* the failure happened, it's true 100% of the time, on every sample, regardless of clock time (confirmed by the same Day 9 sweep). Two independently reasonable rules collide: "retry fast for technical failures" and "wait at least a day between attempts on rails without NPCI's specific UPI cadence rules." Neither rule is wrong on its own. This was deliberately left unfixed — the constraint store's job is to veto blindly, and a planner that can't find a valid sequence correctly escalates rather than inventing one; the fallback agent even has a dedicated `rail_switch_recommended` template for exactly this case, recommending the merchant move the customer to UPI Autopay. Fixing it would mean weakening a Day-4 rule that has no clear better replacement, not patching a bug.

## Recovered revenue: a range, not a point estimate, and why

`app/eval.py`'s simulated recovered revenue is an **expected value under the planner's own hand-specified probability model** — `P(recovered) = 1 − Π(1 − pᵢ)` per retry plan (the correct "at least one attempt succeeds" treatment; deliberately *not* the planner's own additive search-time EV formula, which would double-count revenue across multi-step plans) — times `mandate.amount`, summed across all `retry` decisions. It is not, and should not be presented as, a claim about real-world recovery rates: no real debits are attempted against synthetic mandates, and the probabilities themselves are hand-specified assumptions (documented per-category in `app/planner.py`), not fit from data.

A single run of this number is also not representative on its own. Five independent seeds (`docs/multi_seed_eval.md`) gave a mean of **INR 15,511.57 with a stdev of INR 7,489.26** — a 48% relative stdev, a ~4.6x spread between the luckiest and unluckiest seed, driven by how many events happen to land in retry-eligible categories at a given batch size (retry-plan count itself ranged 16–32 across the same seeds). Quoting Day 8's single-run figure (INR 8,047.30) as if it were the number would have been misleading; the honest claim is the range, with the model-internal-estimate caveat attached every time it's cited.

## S1–S9 system-failure-mode checklist

Every code below has a test that actually exercises the described failure condition — verified by `app/eval.py`'s `run_s_checklist()`, which re-runs the mapped tests via pytest rather than trusting a cached claim, generated fresh into `scripts/eval_report.py`'s output.

| Code | Failure mode | Status | Note |
|---|---|---|---|
| S1 | LLM classifier returns malformed/unparseable output | **Passing** | Fallback to conservative default (ambiguous, one cautious retry), logged |
| S2 | LLM classifier is confidently wrong | **Passing**, with a scope nuance | The literal scenario (LLM mislabels a hard-fail) can't occur here — hard-fail categories never reach the LLM path by construction. The required hard ceiling on retry attempts is real and enforced independently of classifier output |
| S3 | Retry storm / duplicate scheduling | **Passing** | Deterministic idempotency key, unique-constraint collision caught and logged, not a crash |
| S4 | Double-charge race condition | **Passing** | Proven with an actual concurrent race (5 threads, real file-backed DB, `threading.Barrier`), not a sequential call |
| S5 | Razorpay API unavailable/rate-limited | **Passing** | Exponential backoff + jitter, capped, tested against simulated 5xx/429, distinct from mandate-level retry |
| S6 | Constraint store and planner disagree | **Passing**, with an implementation nuance | Constraints are checked *during* candidate generation, not as a separate post-plan pass — functionally equivalent (no invalid plan is ever produced), architecturally different from a literal two-phase pipeline |
| S7 | Planner produces a negative-EV plan | **Passing** (closed Day 9) | A tiny-amount mandate drives the exact `EV_ESCALATE_THRESHOLD_PAISE` branch, proven distinct from the other escalate paths |
| S8 | Fallback agent generates an inappropriate message | **Passing** | Malformed output and a deliberately ungrounded (wrong-category) response both proven to hit the safe default, not ship |
| S9 | Audit log write fails mid-pipeline | **Passing** (closed Day 9) | A real SQLAlchemy-level DB write failure (not a mocked exception) proven to fail loudly and never persist a state change without its audit row. Surfaced a related, separate gap — a claimed row could be left stuck in `executing` if the claimer crashes before finishing — closed Day 10 with `reclaim_stuck_executing_rows`, itself going through the same atomic-conditional-UPDATE pattern as the original S4 claim (not a naive UPDATE, which would reintroduce the race) |

## What this system deliberately doesn't do

No new pipeline logic was added after Day 7 (fallback agent) — Days 8–10 are integration, evaluation, stress-testing, and documentation only, and that boundary was kept deliberately: a change that "looked fixable" while writing the eval report or the S1–S9 tests (the P9 structural tension, most notably) was investigated and reported, not silently patched, unless it was a clear, scoped bug (the dead zone) rather than a genuine design tension between two reasonable rules.
