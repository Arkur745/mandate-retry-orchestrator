"""Run the fallback agent over every escalated RetryPlan that doesn't
already have a FallbackMessage, against the real Groq API.

    venv/Scripts/python.exe scripts/generate_fallbacks.py

Requires plans to already exist (seed mandates -> inject failures ->
classify -> plan). Reports the escalation-type distribution and how often
validation actually passed vs. fell back to the safe default.
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.fallback import generate_fallback_message
from app.models import FailureEvent, FallbackMessage, Mandate, PlanDecision, RetryPlan


def main() -> None:
    db = SessionLocal()
    try:
        already = {m.retry_plan_id for m in db.query(FallbackMessage.retry_plan_id)}
        plans = (
            db.query(RetryPlan)
            .filter(RetryPlan.decision == PlanDecision.escalate)
            .filter(~RetryPlan.id.in_(already) if already else True)
            .all()
        )
        if not plans:
            print("No un-processed escalated plans found -- run the Day 2-5 pipeline first.")
            sys.exit(1)

        by_type = Counter()
        by_method = Counter()
        results = []

        for plan in plans:
            event = db.get(FailureEvent, plan.failure_event_id)
            mandate = db.get(Mandate, event.mandate_id)
            msg = generate_fallback_message(db, plan, event, mandate)
            by_type[msg.escalation_type.value] += 1
            by_method[msg.method.value] += 1
            results.append((event.taxonomy_id, mandate.rail.value, msg))

        print(f"Generated {len(plans)} fallback messages.\n")
        print("By escalation type:")
        for t, n in by_type.most_common():
            print(f"  {t}: {n}")
        print("\nBy method:")
        for m, n in by_method.most_common():
            print(f"  {m}: {n}")

        print("\n--- Examples ---")
        seen_types = set()
        for taxonomy_id, rail, msg in results:
            if msg.escalation_type.value in seen_types:
                continue
            seen_types.add(msg.escalation_type.value)
            print(f"\n[{taxonomy_id} / {rail}] escalation_type={msg.escalation_type.value} "
                  f"method={msg.method.value} validation_passed={msg.validation_passed}")
            print(f"  {msg.content}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
