"""Failure simulator: injects synthetic failure_events against P1-P12 from
docs/failure_taxonomy.md, each carrying a ground-truth recoverability label.

Day 2 scope only. This module does not classify, plan, or execute anything —
it just produces the failure_events rows the classifier will eventually
consume. Mandate authorization is out of scope here by design (see
docs/eval_audit.md, 2026-08-23 entry): the simulator assumes it already has
active mandates to work against.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import FailureEvent, Mandate, Rail

ALL_RAILS = frozenset({Rail.upi_autopay, Rail.e_nach, Rail.card_emandate})


def utcnow() -> datetime:
    """Naive-UTC now, matching the naive DateTime columns in app.models."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class Variant:
    """One possible raw_reason_text rendering for a category, with the
    ground-truth label that applies when this specific variant is chosen.
    Most categories use the same label on every variant; P7 is the
    exception (its recoverability genuinely depends on whether the
    notification-acknowledgement window is still open)."""

    template: str
    recoverable: bool | None


@dataclass(frozen=True)
class TaxonomyCategory:
    id: str
    rails: frozenset[Rail]
    weight: float
    variants: tuple[Variant, ...]


def _v(template: str, recoverable: bool | None) -> Variant:
    return Variant(template=template, recoverable=recoverable)


# Realistic, webhook/bank-decline-shaped text per docs/failure_taxonomy.md
# Part 1. Deliberately avoids restating the taxonomy's plain-English category
# names (e.g. "insufficient balance") verbatim — real decline payloads read
# like terse bank/NPCI/network jargon, not a human-written category label,
# so a downstream classifier has to actually parse the text.
TAXONOMY: dict[str, TaxonomyCategory] = {
    "P1": TaxonomyCategory(
        id="P1",
        rails=frozenset({Rail.upi_autopay, Rail.e_nach}),
        weight=25,
        variants=(
            _v(
                "UPI_AUTOPAY_DEBIT_FAILED: NPCI RC=51 at issuing bank. Debit "
                "declined at presentment; available balance below mandate "
                "amount at time of auto-debit.",
                True,
            ),
            _v(
                "e-NACH debit returned unpaid by destination bank. Reason "
                "code: INSUFF_FUNDS. Clearing house remark: 'Funds "
                "Insufficient'.",
                True,
            ),
            _v(
                "Autopay attempt failed. Bank response: 'Transaction "
                "declined - balance in account is less than transaction "
                "amount.'",
                True,
            ),
        ),
    ),
    "P2": TaxonomyCategory(
        id="P2",
        rails=ALL_RAILS,
        weight=20,
        variants=(
            _v(
                "NPCI_TIMEOUT: no response received from issuing bank within "
                "SLA window (30000ms). Debit marked as failed.",
                True,
            ),
            _v(
                "Gateway error while contacting issuer switch: connection "
                "timed out (read timeout after 25000ms). Upstream bank "
                "system unreachable.",
                True,
            ),
            _v(
                "Debit request expired before bank acknowledgement. NPCI "
                "RC=ZM (Transaction Timed Out).",
                True,
            ),
        ),
    ),
    "P3": TaxonomyCategory(
        id="P3",
        rails=ALL_RAILS,
        weight=5,
        # Ground truth revised 2026-08-24 (Day 10) -- see docs/failure_taxonomy.md's
        # dated revision note. Was uniformly True ("cautious yes") for all
        # three variants; a real classifier investigation (docs/eval_audit.md,
        # Day 9 Part B) found the two explicit-fraud variants consistently
        # and defensibly judged non-recoverable (RC=59-style "Suspected
        # Fraud" is standard bank/network language for a hold needing
        # human/bank-side resolution, not something a blind retry fixes),
        # while the generic-risk-hold variant remains recoverable. Variant
        # identity now carries the label, not the bare category code.
        variants=(
            _v(
                "Transaction held for review by issuing bank's risk engine. "
                "Bank response: 'Declined - Suspected Fraud, contact your "
                "bank.'",
                False,
            ),
            _v(
                "Debit blocked: issuer flagged this transaction under "
                "velocity/risk rules. RC=59 (Suspected Fraud).",
                False,
            ),
            _v(
                "Payment declined by issuer risk system. No further detail "
                "provided by bank; customer may need to confirm the "
                "transaction directly with their bank.",
                True,
            ),
        ),
    ),
    "P4": TaxonomyCategory(
        id="P4",
        rails=ALL_RAILS,
        weight=4,
        variants=(
            _v(
                "Debit rejected: mandate validity period has lapsed "
                "(expire_at < debit_date). No further debits permitted "
                "against this mandate.",
                False,
            ),
            _v(
                "e-NACH registration expired at destination bank. Reason: "
                "'Mandate Expired - Re-registration Required.'",
                False,
            ),
            _v(
                "UPI Autopay mandate is no longer active: validity end date "
                "has passed.",
                False,
            ),
        ),
    ),
    "P5": TaxonomyCategory(
        id="P5",
        rails=ALL_RAILS,
        weight=4,
        variants=(
            _v(
                "Mandate cancelled by customer via bank app. Debit rejected: "
                "'Mandate Revoked by Customer.'",
                False,
            ),
            _v(
                "e-NACH stopped: customer submitted a stop-payment "
                "instruction at their bank branch.",
                False,
            ),
            _v(
                "UPI Autopay mandate paused by user in UPI app. Debit "
                "blocked until mandate is reactivated by the customer.",
                False,
            ),
        ),
    ),
    "P6": TaxonomyCategory(
        id="P6",
        rails=frozenset({Rail.upi_autopay}),
        weight=3,
        variants=(
            _v(
                "UPI Autopay debit exceeds the per-mandate limit configured "
                "at registration. RC=UD (Mandate Amount Limit Exceeded).",
                False,
            ),
            _v(
                "Debit rejected by NPCI: requested amount above the maximum "
                "amount authorized under this UPI Autopay mandate.",
                False,
            ),
        ),
    ),
    "P7": TaxonomyCategory(
        id="P7",
        rails=frozenset({Rail.upi_autopay}),
        weight=8,
        variants=(
            _v(
                "Pre-debit notification sent to customer; 24h acknowledgement "
                "window is still open. Debit deferred pending ack, not yet "
                "failed outright.",
                True,
            ),
            _v(
                "Pre-debit notification not acknowledged within the required "
                "24h window; NPCI rejected the debit as unnotified. RC=U67.",
                False,
            ),
        ),
    ),
    "P8": TaxonomyCategory(
        id="P8",
        rails=frozenset({Rail.card_emandate}),
        weight=6,
        variants=(
            _v(
                "Card e-mandate debit declined: card on file has expired. "
                "Customer must re-tokenize with a valid card.",
                False,
            ),
            _v(
                "Issuer declined recurring charge: card reported "
                "lost/blocked. RC=41 (Lost Card).",
                False,
            ),
        ),
    ),
    "P9": TaxonomyCategory(
        id="P9",
        rails=frozenset({Rail.card_emandate}),
        weight=10,
        variants=(
            _v(
                "Recurring charge failed: issuing bank's authorization "
                "system was unreachable during the scheduled debit window. "
                "RC=91 (Issuer Unavailable).",
                True,
            ),
            _v(
                "Card network reported issuer switch down for scheduled "
                "maintenance; auto-debit could not be processed.",
                True,
            ),
        ),
    ),
    "P10": TaxonomyCategory(
        id="P10",
        rails=frozenset({Rail.e_nach}),
        weight=12,
        variants=(
            _v(
                "e-NACH debit submitted to clearing house; settlement "
                "pending, within normal T+2 clearing cycle. No final "
                "response yet from destination bank.",
                True,
            ),
            _v(
                "NACH debit instruction is in the physical clearing cycle; "
                "final status expected within 2-3 business days per the RBI "
                "clearing calendar.",
                True,
            ),
        ),
    ),
    "P11": TaxonomyCategory(
        id="P11",
        rails=ALL_RAILS,
        weight=2,
        variants=(
            _v(
                "Debit rejected: destination account frozen pending KYC "
                "re-verification. Bank remark: 'Account Frozen - KYC "
                "Non-Compliance.'",
                False,
            ),
            _v(
                "Issuing bank has placed a hold on the customer's account "
                "(RC=48, 'Account Restricted'); reason not disclosed beyond "
                "a compliance hold.",
                False,
            ),
        ),
    ),
    "P12": TaxonomyCategory(
        id="P12",
        rails=ALL_RAILS,
        weight=7,
        variants=(
            _v(
                "Debit declined. Bank response code: RC=96 (System "
                "Malfunction) - no further description provided by issuer.",
                None,
            ),
            _v(
                "Unrecognized decline reason from issuer: raw response "
                "'ERR_UNSPECIFIED_47'. No mapping available in standard "
                "NPCI/bank code tables.",
                None,
            ),
            _v(
                "Debit failed with an unusual combination of flags from the "
                "issuer switch; message text truncated in webhook payload: "
                "'DENIE...TRY LATER PLS CONT'",
                None,
            ),
        ),
    ),
}


def categories_for_rail(rail: Rail) -> list[TaxonomyCategory]:
    return [c for c in TAXONOMY.values() if rail in c.rails]


def _pick_category(rail: Rail, rng: random.Random) -> TaxonomyCategory:
    candidates = categories_for_rail(rail)
    weights = [c.weight for c in candidates]
    return rng.choices(candidates, weights=weights, k=1)[0]


def inject_failure(
    db: Session,
    mandate: Mandate,
    *,
    taxonomy_id: str | None = None,
    rng: random.Random | None = None,
    occurred_at: datetime | None = None,
    commit: bool = True,
) -> FailureEvent:
    """Create one failure_events row for `mandate`.

    If `taxonomy_id` is given, it must be a category applicable to the
    mandate's rail (used for eval / forced Day-9 stress-test scenarios).
    Otherwise a category is sampled, weighted toward real-world frequency,
    from the categories applicable to the mandate's rail.
    """
    rng = rng or random
    if taxonomy_id is not None:
        category = TAXONOMY.get(taxonomy_id)
        if category is None:
            raise ValueError(f"Unknown taxonomy_id: {taxonomy_id!r}")
        if mandate.rail not in category.rails:
            raise ValueError(
                f"{taxonomy_id} does not apply to rail {mandate.rail.value!r} "
                f"(applies to: {sorted(r.value for r in category.rails)})"
            )
    else:
        category = _pick_category(mandate.rail, rng)

    variant = rng.choice(category.variants)

    event = FailureEvent(
        mandate_id=mandate.id,
        taxonomy_id=category.id,
        raw_reason_text=variant.template,
        ground_truth_recoverable=variant.recoverable,
        occurred_at=occurred_at or utcnow(),
    )
    db.add(event)
    if commit:
        db.commit()
        db.refresh(event)
    else:
        db.flush()
    return event


def inject_batch(
    db: Session,
    mandates: list[Mandate],
    count: int,
    *,
    taxonomy_id: str | None = None,
    rng: random.Random | None = None,
) -> list[FailureEvent]:
    """Inject `count` failure events, one mandate drawn at random per event
    (uniformly across mandates whose rail supports the resolved category).
    """
    rng = rng or random
    events: list[FailureEvent] = []

    if taxonomy_id is not None:
        category = TAXONOMY.get(taxonomy_id)
        if category is None:
            raise ValueError(f"Unknown taxonomy_id: {taxonomy_id!r}")
        eligible = [m for m in mandates if m.rail in category.rails]
        if not eligible:
            raise ValueError(
                f"No mandates on a rail compatible with {taxonomy_id} "
                f"(needs one of {sorted(r.value for r in category.rails)})"
            )
        for _ in range(count):
            mandate = rng.choice(eligible)
            events.append(
                inject_failure(db, mandate, taxonomy_id=taxonomy_id, rng=rng, commit=False)
            )
    else:
        for _ in range(count):
            mandate = rng.choice(mandates)
            events.append(inject_failure(db, mandate, rng=rng, commit=False))

    db.commit()
    for event in events:
        db.refresh(event)
    return events
