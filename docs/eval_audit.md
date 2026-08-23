# Eval / stress-test audit log

Chronological log of bugs found, failure modes triggered, and fixes made - written as Day 9 (stress-test day) proceeds. Format: one entry per finding, dated, with before/after behavior.

---

## 2026-08-23 (Day 1) — Razorpay Subscriptions product gated on this test account

**Finding:** Ran `scripts/seed_test_mandate.py` against real test-mode keys to confirm SDK/credential connectivity. `client.customer.create()` succeeded cleanly. `client.plan.create()` and `client.subscription.create()` (and the raw `POST /v1/plans` / `/v1/subscriptions` endpoints) both returned `401 {"error": "Unauthorized"}`. This is not a bad-credentials problem — the same key pair succeeds against `/v1/customers` and `/v1/orders` — it's a product-level gate: the Subscriptions product isn't enabled for a fresh, non-activated test account.

**Before:** Assumed the Subscriptions API (Plan → Subscription) would be the mandate-setup path, since it's Razorpay's documented recurring-billing product.

**After:** Switched the working path to the lower-level Orders + Token API — `POST /v1/orders` with `method="emandate"` and a `token` block returns `200` with the token echoed back, so mandate *registration requests* are reachable on this account without waiting on Subscriptions activation.

**Residual constraint (not a bug, a hard limit):** Neither path lets a script complete mandate *authorization*. That step requires the customer to load a hosted Razorpay Checkout page and approve via netbanking / UPI app / debit card — there is no headless API call that finishes it. Design consequence for the simulator (Day 2): treat an authorized, active mandate as seeded starting state rather than something the pipeline produces end to end.
