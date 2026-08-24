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
