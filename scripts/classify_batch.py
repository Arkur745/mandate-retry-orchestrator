"""Run the classifier over every failure_events row that doesn't already
have a classification, against the real Groq API.

    venv/Scripts/python.exe scripts/classify_batch.py

Reports what fraction of the batch resolved by rule vs. required an LLM
call -- the evidence for "the LLM is only load-bearing where it has to be."
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.classifier import classify_failure
from app.db import SessionLocal
from app.models import Classification, FailureEvent, Mandate


def main() -> None:
    db = SessionLocal()
    try:
        already_classified = {c.failure_event_id for c in db.query(Classification.failure_event_id)}
        events = (
            db.query(FailureEvent)
            .filter(~FailureEvent.id.in_(already_classified) if already_classified else True)
            .all()
        )
        if not events:
            print("No unclassified failure_events found -- run scripts/inject_failures.py first.")
            sys.exit(1)

        mandates_by_id = {m.id: m for m in db.query(Mandate).all()}

        by_method: Counter[str] = Counter()
        by_category_method: Counter[tuple[str, str]] = Counter()
        correct = 0
        scored = 0

        for event in events:
            mandate = mandates_by_id[event.mandate_id]
            classification = classify_failure(db, event, mandate)
            by_method[classification.method.value] += 1
            by_category_method[(event.taxonomy_id, classification.method.value)] += 1
            if event.ground_truth_recoverable is not None and classification.recoverable is not None:
                scored += 1
                if classification.recoverable == event.ground_truth_recoverable:
                    correct += 1

        total = len(events)
        llm_related = by_method.get("llm", 0) + by_method.get("llm_fallback", 0)
        print(f"Classified {total} failure_events.\n")
        print("By method:")
        for method, count in by_method.most_common():
            print(f"  {method}: {count} ({100 * count / total:.1f}%)")
        print(f"\nLLM-related (llm + llm_fallback) fraction: {100 * llm_related / total:.1f}%")
        print(f"Rule-resolved fraction: {100 * by_method.get('rule', 0) / total:.1f}%")

        print("\nBy category x method:")
        for (cat_id, method), count in sorted(by_category_method.items(), key=lambda kv: (int(kv[0][0][1:]), kv[0][1])):
            print(f"  {cat_id} / {method}: {count}")

        if scored:
            print(f"\nAgreement with ground_truth_recoverable (where both are non-null): "
                  f"{correct}/{scored} ({100 * correct / scored:.1f}%)")
    finally:
        db.close()


if __name__ == "__main__":
    main()
