# Eval / stress-test audit log

Chronological log of bugs found, failure modes triggered, and fixes made - written as Day 9 (stress-test day) proceeds. Format: one entry per finding, dated, with before/after behavior.

---

## 2026-08-23 (Day 1) — Razorpay Subscriptions product gated on this test account

**Finding:** Ran `scripts/seed_test_mandate.py` against real test-mode keys to confirm SDK/credential connectivity. `client.customer.create()` succeeded cleanly. `client.plan.create()` and `client.subscription.create()` (and the raw `POST /v1/plans` / `/v1/subscriptions` endpoints) both returned `401 {"error": "Unauthorized"}`. This is not a bad-credentials problem — the same key pair succeeds against `/v1/customers` and `/v1/orders` — it's a product-level gate: the Subscriptions product isn't enabled for a fresh, non-activated test account.

**Before:** Assumed the Subscriptions API (Plan → Subscription) would be the mandate-setup path, since it's Razorpay's documented recurring-billing product.

**After:** Switched the working path to the lower-level Orders + Token API — `POST /v1/orders` with `method="emandate"` and a `token` block returns `200` with the token echoed back, so mandate *registration requests* are reachable on this account without waiting on Subscriptions activation.

**Residual constraint (not a bug, a hard limit):** Neither path lets a script complete mandate *authorization*. That step requires the customer to load a hosted Razorpay Checkout page and approve via netbanking / UPI app / debit card — there is no headless API call that finishes it. Design consequence for the simulator (Day 2): treat an authorized, active mandate as seeded starting state rather than something the pipeline produces end to end.

---

## 2026-08-24 (Day 8) — `fast_technical` timing profile has a ~4.5h daily dead zone, on ANY rail, not just non-UPI

**Finding:** `scripts/run_pipeline.py`'s first full 250-event run (started ~17:05 IST) showed **100% of P2 (bank/NPCI timeout) retry plans escalating** — including on `upi_autopay`, which has none of the `e_nach`/`card_emandate` spacing-floor issue already known from Day 5/6 (see the earlier P9 report). This was surprising: `app/planner.py`'s Day 5 unit tests use `occurred_at` fixtures that happen to sit safely inside the 13:00–17:00 IST non-peak window, so they never exercised this.

**Root cause, confirmed directly** (`app.planner._search_candidates`): the `fast_technical` profile's first two slots offer offsets of only 0.5h and 1.0h. `app.constraints`' UPI non-peak windows are 13:00–17:00 and 21:30–10:00 IST. For an event whose `occurred_at` clock time falls roughly between **16:00 and 20:30 IST**, `occurred_at + 0.5h` and/or `occurred_at + 1.0h` land inside the blocked 17:00–21:30 peak window — and since candidate sequences only use consecutive slots starting at slot 0 (no skipping the fast retry), the *entire* search space is vetoed at the first step, regardless of rail. Verified directly: `occurred_at` at 17:05 IST → 0 valid candidates for P2/`upi_autopay`; the same category/mandate at 14:30 IST → 8 valid candidates.

**Scope, relative to the known Day 5/6 P9 finding:** that finding was rail-specific (e_nach/card_emandate's spacing floor). This one is **time-of-day-specific and rail-independent** — it affects P2 (any rail) and P9 (card_emandate) alike, for roughly 4.5 hours of every 24, including on UPI where no other known issue exists. It's a materially bigger gap than previously understood.

**Not fixed (Day 8 is eval/report only, per scope) — flagged for Day 9.** Candidate fix directions to evaluate then, not decided now: widen `fast_technical`'s first-slot offsets so at least one choice can always clear a 4-hour-wide window regardless of `occurred_at`, or have the search consider slot 3 in isolation (skip-ahead) when slots 1-2 are infeasible, or something else — deliberately not choosing here.

---

## 2026-08-24 (Day 9, Part A) — Dead zone was NOT isolated to `fast_technical`; fixed with window-aware candidate shifting

**Deliberate full sweep** (not accidental discovery this time): tested every `(category, rail)` combination the simulator can actually produce, across a full 24h cycle in 1-minute increments, counting how many `occurred_at` values yield zero valid candidates. Result — the Day 8 finding was an undercount:

| Category / rail | Before fix |
|---|---|
| P1 (`delayed_funds`) / upi_autopay | **Dead 31.2%** of the day: 10:00–13:00, 17:00–21:30 IST |
| P2 (`fast_technical`) / upi_autopay | **Dead 27.1%**: 09:30–12:00, 16:30–20:30 IST |
| P3 (`cautious_single`) / upi_autopay | **Dead 31.2%**: 10:00–13:00, 17:00–21:30 IST |
| P7 (`notification_window`) / upi_autopay | **Dead 31.2%**: 09:00–12:00, 16:00–20:30 IST |
| P12 (`cautious_single`) / upi_autopay | **Dead 31.2%**: 10:00–13:00, 17:00–21:30 IST |
| P1 / e_nach, P3 / card_emandate or e_nach, P10 / e_nach, P12 / card_emandate or e_nach | Clean |
| P2 / e_nach, P2 / card_emandate, P9 / card_emandate | **Always dead, 100%, all day** |

**Root cause, general form:** `delayed_funds`'s 24h/72h/168h offsets and `cautious_single`'s 48h offset are all exact multiples of 24h — they land at the *identical* IST clock time as `occurred_at` itself. If that time-of-day happens to be a UPI peak window, *every* offset in the profile is equally bad (not just some), so the whole profile goes dead, not just degrades. `fast_technical`/`notification_window`'s sub-day offsets create their own, differently-shaped dead windows for the same underlying reason (fixed relative offset, blind to where the actual window boundaries are).

**The always-dead 100% rows (P2/e_nach, P2/card_emandate, P9/card_emandate) are a different, separate thing and were deliberately left untouched**: those are the Day 5/6 finding — `e_nach`/`card_emandate`'s 24h minimum-spacing floor (measured from the original failure) is simply incompatible with `fast_technical`'s sub-day first offset, regardless of what time of day it is. That's a principled collision between two independently-reasonable rules; the partial dead zones above are not — same category, same rail, same mandate, and the only variable was clock time. A merchant has no defensible answer for "why did my 4pm outage auto-retry but my 5pm one didn't."

**Fix:** `app.constraints.next_non_peak_window_start(dt_utc, rail)` — a new, pure utility function, *not* a change to `check_retry`'s veto behavior (the constraint store still blindly rejects a bad proposal exactly as before; Day 4's own design comment already anticipated this split: *"Re-proposing a corrected timestamp is the planner's job, not this module's."*). `app.planner._search_candidates` now runs each candidate timestamp through this before checking it: if a rail enforces non-peak windows and the raw `occurred_at + offset` lands in a peak window, it's shifted forward to that window's next opening, with ordering preserved across the sequence (a later step's raw anchor is bumped forward if an earlier step's shift pushed past it).

**Verified:** re-ran the identical full-day sweep after the fix. All five partial dead zones (P1/P2/P3/P7/P12 on upi_autopay) are now clean at every sampled minute. The three always-dead rows are byte-for-byte unchanged (still 100% dead) — confirming the fix is surgical and doesn't paper over the real structural collision. Regression tests: `tests/test_planner.py::TestDeadZoneFix` (same P2/upi_autopay category+mandate at the old dead-zone time and a known-safe time, both now plan successfully; shifted attempts verified to land in a non-peak window; P9/card_emandate confirmed to still escalate identically at both times) and `tests/test_constraints.py` (5 new unit tests for `next_non_peak_window_start` directly).

---

## 2026-08-24 (Day 9, Part B) — P3 classifier "disagreement" investigated; NOT fixed, decision deferred

**Investigation only — no prompt or ground-truth change made, per explicit instruction.** Day 8's eval report showed P3 classifier accuracy at 15.8% (aggregate run) — re-generated a focused batch of 25 real P3 events, classified against the real Groq API (not mocked): 21/24 scored disagreements (12.5% agreement), consistent with Day 8.

**Read the actual `reasoning` field for every disagreement, not just the verdict.** The disagreement is not noise — it is perfectly correlated with which of the simulator's three P3 `raw_reason_text` templates (`app/simulator.py`) was used:

- *"Transaction held for review by issuing bank's risk engine. Bank response: 'Declined - Suspected Fraud, contact your bank.'"* → Groq says `recoverable=False`, **every single time** (11/11 in this batch). Example reasoning: *"Suspected fraud flag by issuing bank makes retry unlikely to succeed."* / *"Bank flagged suspected fraud, retry unlikely to succeed."*
- *"Debit blocked: issuer flagged this transaction under velocity/risk rules. RC=59 (Suspected Fraud)."* → also `recoverable=False`, **every single time** (10/10). Example: *"Issuer flagged the transaction as suspected fraud under velocity/risk rules, so retrying is unlikely to succeed."*
- *"Payment declined by issuer risk system. No further detail provided by bank; customer may need to confirm the transaction directly with their bank."* → `recoverable=True`, **every single time** (3/3). Example: *"Issuer risk block is often temporary and can be resolved by customer confirmation, so retrying may succeed."*

**Read on which side is closer to right:** the two "false" variants both explicitly say **"Suspected Fraud"** in the bank's own stated decline reason (one even cites RC=59, a real card-network suspected-fraud response code). Treating an explicit suspected-fraud flag as a hard block needing bank-side/human resolution — not something a blind retry fixes — is standard real-world payments practice, not a keyword-matching artifact; the model differentiates cleanly and consistently rather than reacting to the mere presence of "risk"/"fraud"-adjacent words (the third variant, which mentions "risk system" but not fraud specifically and describes a customer-confirmation path, gets the opposite verdict every time). This reads more like the classifier catching a real distinction the taxonomy's single `ground_truth_recoverable=True` label for *all* P3 variants doesn't make, than like the classifier being simply wrong. Worth noting: `app/fallback.py`'s own `CATEGORY_SIGNAL_TERMS` for P3 already lists `"fraud"`, `"risk hold"`, `"suspicious activity"` as the category's defining language — built independently on Day 7, before this investigation — which suggests the "explicit fraud" framing was already recognized as P3's core identity elsewhere in this codebase.

**Not acted on.** Per instruction, this is a report for a decision the user makes, not something to fix or tune unilaterally — see chat transcript for the full writeup and options.

---

## 2026-08-24 (Day 9, Part C) — S7 and S9 now have real tests; S9 test surfaced a related, separate gap

**S7** (`tests/test_planner.py::TestNegativeExpectedValueEscalates`): a mandate with a tiny amount (INR 1.00) makes `BASE_COST_PAISE` (200) exceed `p * amount` for every candidate in every profile — confirmed directly: `cautious_single`'s single candidate gives `0.45*100 - 200 = -155` paise. Escalates with `escalation_reason_code=negative_expected_value`, distinct from the "no search attempted" and "all vetoed" escalate paths (asserted explicitly).

**S9** (`tests/test_executor.py`, two new tests): simulated a genuine DB-level write failure, not a mocked Python exception — a SQLAlchemy `before_cursor_execute` hook that intercepts the actual SQL reaching the DBAPI and raises `sqlite3.OperationalError` only for `INSERT INTO audit_log`, leaving every other statement untouched. Confirmed: the pipeline fails loudly (the exception propagates out of `claim_and_execute`, not swallowed), and no misleading state is persisted — `retry_attempts.outcome` never reaches `success`/`failed` without its paired audit_log row, because both are one transaction that rolls back together.

**Related finding, not fixed (out of Part C's scope — "write tests," not "add new recovery behavior"):** the same test revealed that the row is left permanently stuck in `executing` state. This happens because `claim_and_execute`'s atomic claim (`pending` → `executing`) is its own separate, already-committed transaction *before* the audit-log-write failure occurs later in the same call — so the claim survives even though the subsequent state-change-plus-audit-log commit rolls back. The exception does propagate (satisfying S9's literal "fail loudly" requirement), but there's currently no mechanism to detect or recover a retry_attempt stuck in `executing` (e.g. a sweep that reclaims rows stuck in `executing` past some timeout). Flagging for a future day — this is a slightly different failure mode than S9 as literally scoped, not something Part C asked for.

---

## 2026-08-24 (Day 10) — P3 ground truth corrected; accuracy 15.8% → 100.0%

Per the Day 9 Part B investigation and the user's decision: `app/simulator.py`'s P3 variants now carry per-variant `ground_truth_recoverable` (two explicit-"Suspected Fraud" variants → `False`, the generic-risk-hold variant → `True`) instead of a uniform `True`. Full rationale and the revision note are in `docs/failure_taxonomy.md`. No classifier or prompt code touched.

**Re-ran classification on a fresh 30-event P3 batch against the real Groq API** (ground truth now 6 True / 24 False, matching the ~1:2 variant weighting): **26/26 scored agreement = 100.0%** (4/30 hit the S1 fallback path — unscored, consistent with the known Groq `max_tokens`-exhaustion failure mode seen on Day 3/7, not a new issue). Up from the pre-fix 15.8% (3/19).
