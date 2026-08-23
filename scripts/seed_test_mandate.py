"""Day-1 connectivity check against Razorpay's test-mode API.

Not part of the orchestrator — standalone, run manually:

    venv/Scripts/python.exe scripts/seed_test_mandate.py

Goal: prove RAZORPAY_KEY_ID/SECRET and the `razorpay` SDK work end to end,
and find out what a fresh test-mode account can actually do toward setting
up a recurring-payment mandate. Findings from this script (see README run
log / commit message):

1. Customer creation works fine (SDK `client.customer.create`).
2. The Subscriptions product (`client.plan.create` / `client.subscription.create`,
   and the raw `/v1/plans`, `/v1/subscriptions` REST endpoints) returns
   401 {"error": "Unauthorized"} on this account — not a bad-credentials
   error (customers/orders calls succeed with the same key), but a
   product-level gate. Subscriptions has to be enabled for the merchant
   account (via Dashboard, sometimes requiring business activation) before
   the API accepts calls, even in test mode.
3. The lower-level Orders + Token API (POST /v1/orders with
   method="emandate" and a `token` block) is NOT gated the same way —
   it returns 200 and echoes back a token object, i.e. Razorpay accepts
   the mandate-registration *request*. This script uses that path since
   it's the one this account can actually exercise.
4. Regardless of which path is used, this only creates the registration
   *request*. Actual authorization (the customer approving the mandate via
   netbanking / UPI app / debit card) happens on a hosted Razorpay Checkout
   page — there is no server-side-only call that completes it. That's the
   real constraint for the Day-2 failure simulator: we can drive mandate
   *creation* headlessly, but not mandate *authorization*, so the
   simulator will need to treat "authorized mandate" as a seeded/assumed
   starting state rather than something this script can produce.

Every step prints the raw SDK/HTTP response verbatim. Failures are not
routed around — a failure here is itself the Day-2 design input.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import razorpay
from razorpay.errors import BadRequestError, GatewayError, ServerError

from app.db import settings

AUTH = None  # set in main() once keys are confirmed present


def dump(label: str, obj) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(obj, indent=2, default=str))


def main() -> None:
    global AUTH
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        print("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set in .env — aborting.")
        sys.exit(1)

    AUTH = (settings.razorpay_key_id, settings.razorpay_key_secret)
    client = razorpay.Client(auth=AUTH)
    print(f"Using key_id={settings.razorpay_key_id} (test mode: {'test' in settings.razorpay_key_id})")

    # Step 1: create a test customer via the SDK.
    try:
        customer = client.customer.create(
            {
                "name": "Mandate Orchestrator Test Customer",
                "email": "test.customer@example.com",
                "contact": "9999999999",
                "fail_existing": "0",
            }
        )
        dump("customer.create (SDK)", customer)
    except (BadRequestError, ServerError, GatewayError) as exc:
        dump("customer.create FAILED", {"error": str(exc)})
        print("\nCould not even create a customer — aborting, nothing downstream will work.")
        sys.exit(1)

    # Step 2: try the Subscriptions product (Plan -> Subscription). Expected,
    # per the findings above, to fail with 401 on an unactivated account —
    # reported as-is, not worked around.
    try:
        plan = client.plan.create(
            {
                "period": "monthly",
                "interval": 1,
                "item": {
                    "name": "Mandate Retry Orchestrator - Test Plan",
                    "amount": 50000,
                    "currency": "INR",
                    "description": "Day-1 connectivity check plan",
                },
            }
        )
        dump("plan.create (SDK)", plan)
    except (BadRequestError, ServerError, GatewayError) as exc:
        dump("plan.create FAILED (expected: Subscriptions product not enabled)", {"error": str(exc)})
        raw = httpx.post("https://api.razorpay.com/v1/plans", auth=AUTH, json={})
        dump("raw POST /v1/plans (for exact status/body)", {"status": raw.status_code, "body": raw.json()})

    # Step 3: the path that actually works on this account — Orders API
    # with method="emandate" and a token block, representing an e-NACH
    # mandate registration request.
    order_resp = httpx.post(
        "https://api.razorpay.com/v1/orders",
        auth=AUTH,
        json={
            "amount": 100,
            "currency": "INR",
            "customer_id": customer["id"],
            "method": "emandate",
            "token": {
                "max_amount": 50000,
                "expire_at": 1893456000,  # 2030-01-01, arbitrary far future for this check
                "auth_type": "netbanking",
            },
        },
    )
    dump("POST /v1/orders (method=emandate, token) - mandate registration request", {
        "status": order_resp.status_code,
        "body": order_resp.json(),
    })

    if order_resp.status_code == 200:
        print(
            "\nOrder created with a token block echoed back — Razorpay accepted the "
            "mandate-registration request. There is no further headless step: "
            "completing authorization requires the customer to visit a hosted "
            "Razorpay Checkout page and approve via netbanking/UPI app/debit card. "
            "This script stops here by design — that page load is not something "
            "a backend script should (or can) drive."
        )

    print("\nDone. Findings are printed above verbatim — see module docstring for the summary.")


if __name__ == "__main__":
    main()
