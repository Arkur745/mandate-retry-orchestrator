"""Seed a batch of synthetic, already-active mandates for the Day-2
failure simulator to inject against.

    venv/Scripts/python.exe scripts/seed_mandates.py --count 30
    venv/Scripts/python.exe scripts/seed_mandates.py --count 30 --with-real-auth-link

Mandate authorization is a one-time, human click-through step that can't be
scripted (see docs/eval_audit.md, 2026-08-23 entry) -- these mandates are
created directly in the DB with status=active rather than driving Razorpay's
real auth flow. `--with-real-auth-link` additionally generates one real
Razorpay e-mandate registration link (via the Registration Link / auth_links
API, which -- unlike Subscriptions -- is not gated on this account) for one
seeded mandate, and logs it to audit_log for later manual click-through. It
does not and cannot complete that click-through itself.
"""
import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import timedelta

import httpx

from app.db import SessionLocal, init_db, settings
from app.models import AuditLog, Mandate, MandateStatus, Rail
from app.simulator import utcnow

AMOUNTS_PAISE = [9900, 19900, 29900, 49900, 99900, 199900]  # INR 99 - 1999

# Rough real-world rail mix for India recurring payments in 2026: UPI
# Autopay dominant, card e-mandate second, e-NACH declining but still used
# for larger-ticket / longer-tenure recurring debits (loans, insurance).
RAIL_WEIGHTS = {
    Rail.upi_autopay: 0.5,
    Rail.card_emandate: 0.3,
    Rail.e_nach: 0.2,
}

EXPIRY_DAY_RANGE = {
    Rail.upi_autopay: (180, 545),   # ~6-18 months
    Rail.card_emandate: (365, 900),  # ~1-2.5 years, bounded by card validity
    Rail.e_nach: (365, 1095),        # ~1-3 years
}


def build_mandate(index: int, rng: random.Random) -> Mandate:
    rail = rng.choices(list(RAIL_WEIGHTS), weights=list(RAIL_WEIGHTS.values()), k=1)[0]
    lo, hi = EXPIRY_DAY_RANGE[rail]
    expiry = utcnow() + timedelta(days=rng.randint(lo, hi))
    return Mandate(
        customer_ref=f"cust_synth_{index:04d}",
        rail=rail,
        amount=rng.choice(AMOUNTS_PAISE),
        status=MandateStatus.active,
        mandate_expiry=expiry,
    )


def create_real_auth_link(mandate: Mandate) -> dict:
    """Best-effort: generate one real Razorpay e-mandate registration link
    for `mandate` via the auth_links API. Returns the raw response body.
    Raises on failure -- caller decides whether that's fatal."""
    auth = (settings.razorpay_key_id, settings.razorpay_key_secret)
    resp = httpx.post(
        "https://api.razorpay.com/v1/subscription_registration/auth_links",
        auth=auth,
        json={
            "customer": {
                "name": f"Seed Demo Customer {mandate.customer_ref}",
                "email": "seed.demo.customer@example.com",
                "contact": "9876543210",
            },
            "type": "link",
            "amount": 0,
            "currency": "INR",
            "description": f"Demo authorization for mandate {mandate.id}",
            "subscription_registration": {
                "method": "emandate",
                "max_amount": mandate.amount,
                "expire_at": int(mandate.mandate_expiry.timestamp()),
            },
            "receipt": f"seed-mandate-{mandate.id}-{int(utcnow().timestamp())}",
            "expire_by": int((utcnow() + timedelta(days=7)).timestamp()),
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=30)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--with-real-auth-link",
        action="store_true",
        help="Also generate one real Razorpay registration link for the first seeded mandate.",
    )
    args = parser.parse_args()

    rng = random.Random(args.seed)
    init_db()
    db = SessionLocal()
    try:
        mandates = [build_mandate(i, rng) for i in range(1, args.count + 1)]
        db.add_all(mandates)
        db.commit()
        for m in mandates:
            db.refresh(m)

        by_rail = {rail: sum(1 for m in mandates if m.rail == rail) for rail in Rail}
        print(f"Seeded {len(mandates)} mandates: " + ", ".join(f"{r.value}={n}" for r, n in by_rail.items()))

        if args.with_real_auth_link:
            demo_mandate = mandates[0]
            print(f"\nAttempting real Razorpay registration link for mandate id={demo_mandate.id}...")
            try:
                body = create_real_auth_link(demo_mandate)
                short_url = body.get("short_url")
                db.add(
                    AuditLog(
                        related_entity_type="mandate",
                        related_entity_id=demo_mandate.id,
                        event_type="razorpay_registration_link_created",
                        detail={
                            "invoice_id": body.get("id"),
                            "order_id": body.get("order_id"),
                            "customer_id": body.get("customer_id"),
                            "short_url": short_url,
                            "status": body.get("status"),
                        },
                    )
                )
                db.commit()
                print(f"Registration link created and logged to audit_log: {short_url}")
                print(
                    "This link is real and clickable, but completing mandate authorization "
                    "requires a human to open it and approve via netbanking/UPI app -- "
                    "no headless step exists for that (see docs/eval_audit.md). Not clicked "
                    "through as part of this script run."
                )
            except httpx.HTTPStatusError as exc:
                print(f"Registration link creation failed ({exc.response.status_code}): {exc.response.text}")
                print("Continuing without a real auth link -- mandate remains fully synthetic.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
