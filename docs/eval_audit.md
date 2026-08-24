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
