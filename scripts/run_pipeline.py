"""End-to-end pipeline integration: simulate -> classify -> plan ->
execute -> (fallback if escalated) -> audit, in one continuous run with no
manual intervention between stages.

    venv/Scripts/python.exe scripts/run_pipeline.py --mandate-count 40 --event-count 250

Unlike each day's own isolated batch script (seed_mandates.py,
inject_failures.py, classify_batch.py, generate_fallbacks.py -- each of
which processes *all* outstanding rows of its own stage independently),
this script drives every event through all five stages in a single loop,
so a failure introduced at stage N is immediately visible to stage N+1
without a human re-running anything in between.

Retry execution uses the real app.executor.RetryScheduler / APScheduler
wiring (not a special demo-only path -- see app/executor.py's Clock), just
heavily fast-forwarded via --time-scale so a batch with attempts scheduled
hours to a week out still completes in well under a minute.
"""
import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.classifier import classify_failure
from app.db import SessionLocal, init_db
from app.executor import Clock, RetryScheduler, schedule_retry_plan
from app.fallback import generate_fallback_message
from app.models import (
    AuditLog,
    Classification,
    FailureEvent,
    FallbackMessage,
    Mandate,
    PlanDecision,
    RetryAttempt,
    RetryOutcome,
    RetryPlan,
)
from app.planner import plan_retries
from app.simulator import inject_batch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_mandates import build_mandate  # noqa: E402


def print_mandate_trace(db, mandate_id: int) -> None:
    mandate = db.get(Mandate, mandate_id)
    print(f"\n{'=' * 70}\nFull trace for mandate {mandate_id} "
          f"(rail={mandate.rail.value}, amount={mandate.amount/100:.2f} INR)\n{'=' * 70}")

    events = (
        db.query(FailureEvent)
        .filter_by(mandate_id=mandate_id)
        .order_by(FailureEvent.occurred_at)
        .all()
    )
    for event in events:
        print(f"\n--- failure_event {event.id}: {event.taxonomy_id} "
              f"@ {event.occurred_at} (ground_truth_recoverable={event.ground_truth_recoverable}) ---")
        print(f"    raw_reason_text: {event.raw_reason_text}")

        cls = db.query(Classification).filter_by(failure_event_id=event.id).first()
        if cls:
            print(f"    classification: method={cls.method.value} recoverable={cls.recoverable} "
                  f"confidence={cls.confidence} reasoning={cls.reasoning!r}")

        plan = db.query(RetryPlan).filter_by(failure_event_id=event.id).first()
        if plan:
            print(f"    plan: decision={plan.decision.value} "
                  f"reason_code={plan.escalation_reason_code.value if plan.escalation_reason_code else None} "
                  f"expected_value={plan.expected_value}")
            print(f"          reasoning={plan.reasoning}")
            for step in plan.steps:
                attempt = (
                    db.query(RetryAttempt)
                    .filter_by(failure_event_id=event.id, attempt_number=step.attempt_number)
                    .first()
                )
                outcome = attempt.outcome.value if attempt else "not scheduled"
                print(f"      planned attempt #{step.attempt_number} @ {step.proposed_timestamp} "
                      f"p={step.success_probability:.2f} cost={step.cost:.0f} -> outcome={outcome}")

            for msg in db.query(FallbackMessage).filter_by(retry_plan_id=plan.id).all():
                print(f"    fallback: type={msg.escalation_type.value} method={msg.method.value} "
                      f"validation_passed={msg.validation_passed}")
                print(f"              content={msg.content!r}")

    print("\n    audit_log entries touching this mandate's events:")
    event_ids = {e.id for e in events}
    audit_rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.related_entity_type.in_(["failure_event", "retry_attempt", "retry_plan"]),
        )
        .order_by(AuditLog.created_at)
        .all()
    )
    plan_ids = {p.id for e in events for p in db.query(RetryPlan).filter_by(failure_event_id=e.id).all()}
    attempt_ids = {a.id for a in db.query(RetryAttempt).filter(RetryAttempt.failure_event_id.in_(event_ids)).all()}
    for row in audit_rows:
        if (
            (row.related_entity_type == "failure_event" and row.related_entity_id in event_ids)
            or (row.related_entity_type == "retry_plan" and row.related_entity_id in plan_ids)
            or (row.related_entity_type == "retry_attempt" and row.related_entity_id in attempt_ids)
        ):
            print(f"      [{row.created_at}] {row.event_type} ({row.related_entity_type}#{row.related_entity_id}): {row.detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mandate-count", type=int, default=40)
    parser.add_argument("--event-count", type=int, default=250)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--time-scale", type=float, default=200_000.0)
    parser.add_argument("--max-wait-seconds", type=float, default=60.0)
    parser.add_argument("--trace-mandate-id", type=int, default=None)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    init_db()
    db = SessionLocal()

    print(f"[1/5] Seeding {args.mandate_count} mandates...")
    mandates = [build_mandate(i, rng) for i in range(1, args.mandate_count + 1)]
    db.add_all(mandates)
    db.commit()
    for m in mandates:
        db.refresh(m)
    by_rail = {}
    for m in mandates:
        by_rail[m.rail.value] = by_rail.get(m.rail.value, 0) + 1
    print(f"       {by_rail}")

    print(f"[2/5] Injecting {args.event_count} failure events across P1-P12...")
    events = inject_batch(db, mandates, args.event_count, rng=rng)
    seen_categories = {e.taxonomy_id for e in events}
    print(f"       Categories covered: {sorted(seen_categories, key=lambda c: int(c[1:]))} "
          f"({len(seen_categories)}/12)")

    print("[3/5] Classifying, planning, and dispatching (execute or fallback) each event...")
    clock = Clock(scale=args.time_scale)
    scheduler = RetryScheduler(SessionLocal, clock=clock)
    scheduler.start()

    retry_count = 0
    escalate_count = 0
    scheduled_attempt_ids: list[int] = []

    try:
        for event in events:
            mandate = db.get(Mandate, event.mandate_id)
            cls = classify_failure(db, event, mandate)
            plan = plan_retries(db, event, cls, mandate)

            if plan.decision == PlanDecision.retry:
                retry_count += 1
                rows = schedule_retry_plan(db, plan)
                for row in rows:
                    scheduler.schedule_attempt(row, mandate.id)
                    scheduled_attempt_ids.append(row.id)
            else:
                escalate_count += 1
                generate_fallback_message(db, plan, event, mandate)

        print(f"       {retry_count} retry plans ({len(scheduled_attempt_ids)} attempts scheduled), "
              f"{escalate_count} escalations (fallback messages generated)")

        print(f"[4/5] Waiting for scheduled attempts to execute (fast-forwarded, scale={args.time_scale:.0f})...")
        deadline = time.monotonic() + args.max_wait_seconds
        remaining = len(scheduled_attempt_ids)
        while time.monotonic() < deadline and scheduled_attempt_ids:
            db.expire_all()
            remaining = (
                db.query(RetryAttempt)
                .filter(
                    RetryAttempt.id.in_(scheduled_attempt_ids),
                    RetryAttempt.outcome.in_([RetryOutcome.pending, RetryOutcome.executing]),
                )
                .count()
            )
            if remaining == 0:
                break
            time.sleep(0.25)
        if remaining:
            print(f"       WARNING: {remaining} attempts still not settled after {args.max_wait_seconds}s")
        else:
            print("       All scheduled attempts settled.")
    finally:
        scheduler.shutdown(wait=True)

    db.expire_all()
    outcomes = {}
    for row in db.query(RetryAttempt).filter(RetryAttempt.id.in_(scheduled_attempt_ids)).all():
        outcomes[row.outcome.value] = outcomes.get(row.outcome.value, 0) + 1
    print(f"       retry_attempts outcomes: {outcomes}")

    print("[5/5] Full trace for one mandate, followed end to end:")
    trace_mandate_id = args.trace_mandate_id
    if trace_mandate_id is None:
        # Pick a mandate with both a retry AND an escalation, if one exists,
        # for the richest possible trace; else just the first mandate with
        # any failure_events at all.
        candidate = (
            db.query(FailureEvent.mandate_id)
            .join(RetryPlan, RetryPlan.failure_event_id == FailureEvent.id)
            .group_by(FailureEvent.mandate_id)
            .first()
        )
        trace_mandate_id = candidate[0] if candidate else mandates[0].id
    print_mandate_trace(db, trace_mandate_id)

    db.close()
    print("\nDone. No manual steps were required between stages.")


if __name__ == "__main__":
    main()
