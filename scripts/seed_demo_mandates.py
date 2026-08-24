"""Seeds a general background batch (for the trace viewer's search/filter
to feel real) plus five explicitly-constructed "demo-ready" scenarios,
each chosen to be a clean, unambiguous illustration of one thing:

  1. clean_rule_success      -- P2/upi_autopay, rule-classified, retries,
                                 executes successfully. The boring/good case.
  2. llm_path_p3             -- P3, explicit-fraud variant, classified via
                                 the REAL Groq API (not mocked) -- shows
                                 genuine LLM reasoning text, post the Day 10
                                 ground-truth fix.
  3. rail_switch_p9          -- P9/card_emandate. Always escalates
                                 (structural rail-spacing collision, Day
                                 5/6/9) -> rail_switch_recommended fallback.
  4. dead_zone_fixed         -- P2/upi_autopay with occurred_at at exactly
                                 17:05 IST -- the timestamp that produced 0
                                 valid candidates before the Day 9 fix.
                                 Confirms it now plans successfully.
  5. retry_exhausted_nudge   -- P1/upi_autopay on a tiny-amount mandate
                                 (INR 1.00), forcing negative expected
                                 value (same technique as the Day 9 S7
                                 test) -- this escalation type doesn't
                                 occur naturally at small scale (Day 10
                                 finding), so it's forced deliberately.

Real Groq calls throughout -- no mocking. Run once against a fresh or
existing orchestrator.db; prints the resulting mandate IDs, which are
also written to docs/demo_mandates.md by hand after review.

    venv/Scripts/python.exe scripts/seed_demo_mandates.py
"""
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.classifier import classify_failure
from app.db import SessionLocal, init_db
from app.executor import Clock, claim_and_execute, schedule_retry_plan
from app.fallback import generate_fallback_message
from app.models import FailureEvent, Mandate, MandateStatus, PlanDecision, Rail
from app.planner import plan_retries
from app.simulator import inject_batch, utcnow

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_mandates import build_mandate  # noqa: E402


def run_general_background_batch(db, rng: random.Random) -> None:
    print("[background] Seeding a general batch for realistic search/filter...")
    mandates = [build_mandate(i, rng) for i in range(1, 16)]
    db.add_all(mandates)
    db.commit()
    for m in mandates:
        db.refresh(m)
    events = inject_batch(db, mandates, 30, rng=rng)
    for event in events:
        mandate = db.get(Mandate, event.mandate_id)
        cls = classify_failure(db, event, mandate)
        plan = plan_retries(db, event, cls, mandate)
        if plan.decision == PlanDecision.retry:
            rows = schedule_retry_plan(db, plan)
            for row in rows:
                claim_and_execute(db, row.id, mandate, clock=Clock(scale=1.0))
        else:
            generate_fallback_message(db, plan, event, mandate)
    print(f"[background] {len(mandates)} mandates, {len(events)} events done.")


def make_mandate(db, *, rail: Rail, amount: int, customer_ref: str) -> Mandate:
    mandate = Mandate(
        customer_ref=customer_ref,
        rail=rail,
        amount=amount,
        status=MandateStatus.active,
        mandate_expiry=utcnow() + timedelta(days=365),
    )
    db.add(mandate)
    db.commit()
    db.refresh(mandate)
    return mandate


def scenario_clean_rule_success(db) -> int:
    print("[1/5] clean_rule_success (P2/upi_autopay, rule path, executes)...")
    mandate = make_mandate(db, rail=Rail.upi_autopay, amount=99900, customer_ref="demo_clean_success")
    event = FailureEvent(
        mandate_id=mandate.id,
        taxonomy_id="P2",
        raw_reason_text=(
            "NPCI_TIMEOUT: no response received from issuing bank within SLA window "
            "(30000ms). Debit marked as failed."
        ),
        ground_truth_recoverable=True,
        occurred_at=datetime(2026, 8, 24, 9, 0, 0),  # 14:30 IST -- safely non-peak
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    cls = classify_failure(db, event, mandate)
    plan = plan_retries(db, event, cls, mandate)
    assert plan.decision == PlanDecision.retry, plan.reasoning
    rows = schedule_retry_plan(db, plan)
    for row in rows:
        claim_and_execute(db, row.id, mandate, clock=Clock(scale=1.0))
    return mandate.id


def scenario_llm_path_p3(db) -> int:
    print("[2/5] llm_path_p3 (real Groq call, post ground-truth fix)...")
    mandate = make_mandate(db, rail=Rail.card_emandate, amount=199900, customer_ref="demo_llm_p3")
    event = FailureEvent(
        mandate_id=mandate.id,
        taxonomy_id="P3",
        raw_reason_text=(
            "Transaction held for review by issuing bank's risk engine. Bank response: "
            "'Declined - Suspected Fraud, contact your bank.'"
        ),
        ground_truth_recoverable=False,  # corrected label, Day 10
        occurred_at=datetime(2026, 8, 24, 9, 0, 0),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    cls = classify_failure(db, event, mandate)  # real Groq call, not mocked
    plan_retries(db, event, cls, mandate)
    return mandate.id


def scenario_rail_switch_p9(db) -> int:
    print("[3/5] rail_switch_p9 (structural collision, always escalates)...")
    mandate = make_mandate(db, rail=Rail.card_emandate, amount=299900, customer_ref="demo_rail_switch_p9")
    event = FailureEvent(
        mandate_id=mandate.id,
        taxonomy_id="P9",
        raw_reason_text=(
            "Recurring charge failed: issuing bank's authorization system was unreachable "
            "during the scheduled debit window. RC=91 (Issuer Unavailable)."
        ),
        ground_truth_recoverable=True,
        occurred_at=datetime(2026, 8, 24, 9, 0, 0),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    cls = classify_failure(db, event, mandate)
    plan = plan_retries(db, event, cls, mandate)
    assert plan.decision == PlanDecision.escalate
    msg = generate_fallback_message(db, plan, event, mandate)
    assert msg.escalation_type.value == "rail_switch_recommended", msg.escalation_type
    return mandate.id


def scenario_dead_zone_fixed(db) -> int:
    print("[4/5] dead_zone_fixed (occurred_at = 17:05 IST, the former dead zone)...")
    mandate = make_mandate(db, rail=Rail.upi_autopay, amount=49900, customer_ref="demo_dead_zone_fixed")
    event = FailureEvent(
        mandate_id=mandate.id,
        taxonomy_id="P2",
        raw_reason_text=(
            "Gateway error while contacting issuer switch: connection timed out (read "
            "timeout after 25000ms). Upstream bank system unreachable."
        ),
        ground_truth_recoverable=True,
        occurred_at=datetime(2026, 8, 24, 11, 35, 0),  # 17:05 IST -- exact Day 8/9 dead-zone time
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    cls = classify_failure(db, event, mandate)
    plan = plan_retries(db, event, cls, mandate)
    assert plan.decision == PlanDecision.retry, (
        f"dead zone regression -- expected a valid plan, got escalate: {plan.reasoning}"
    )
    rows = schedule_retry_plan(db, plan)
    for row in rows:
        claim_and_execute(db, row.id, mandate, clock=Clock(scale=1.0))
    return mandate.id


def scenario_retry_exhausted_nudge(db) -> int:
    print("[5/5] retry_exhausted_nudge (forced via tiny amount -> negative EV)...")
    mandate = make_mandate(db, rail=Rail.upi_autopay, amount=100, customer_ref="demo_retry_exhausted_nudge")
    event = FailureEvent(
        mandate_id=mandate.id,
        taxonomy_id="P1",
        raw_reason_text=(
            "Autopay attempt failed. Bank response: 'Transaction declined - balance in "
            "account is less than transaction amount.'"
        ),
        ground_truth_recoverable=True,
        occurred_at=datetime(2026, 8, 24, 9, 0, 0),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    cls = classify_failure(db, event, mandate)
    plan = plan_retries(db, event, cls, mandate)
    assert plan.decision == PlanDecision.escalate
    assert plan.escalation_reason_code.value == "negative_expected_value", plan.escalation_reason_code
    msg = generate_fallback_message(db, plan, event, mandate)
    assert msg.escalation_type.value == "retry_exhausted_nudge", msg.escalation_type
    return mandate.id


def main() -> None:
    init_db()
    db = SessionLocal()
    rng = random.Random(7)
    try:
        run_general_background_batch(db, rng)

        ids = {
            "clean_rule_success": scenario_clean_rule_success(db),
            "llm_path_p3": scenario_llm_path_p3(db),
            "rail_switch_p9": scenario_rail_switch_p9(db),
            "dead_zone_fixed": scenario_dead_zone_fixed(db),
            "retry_exhausted_nudge": scenario_retry_exhausted_nudge(db),
        }

        print("\n=== Demo mandate IDs ===")
        for name, mandate_id in ids.items():
            print(f"  {name}: mandate #{mandate_id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
