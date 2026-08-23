"""Batch-run the failure simulator against seeded mandates.

    venv/Scripts/python.exe scripts/inject_failures.py --count 200
    venv/Scripts/python.exe scripts/inject_failures.py --count 20 --category P4

Requires mandates to already exist (run scripts/seed_mandates.py first).
Prints the resulting per-category distribution so the actual mix used for
this run is auditable, not just the target weights in app/simulator.py.
"""
import argparse
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.models import Mandate
from app.simulator import TAXONOMY, inject_batch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--category",
        choices=sorted(TAXONOMY),
        default=None,
        help="Force every injected event into this taxonomy category.",
    )
    args = parser.parse_args()

    rng = random.Random(args.seed)
    db = SessionLocal()
    try:
        mandates = db.query(Mandate).all()
        if not mandates:
            print("No mandates found -- run scripts/seed_mandates.py first.")
            sys.exit(1)

        events = inject_batch(db, mandates, args.count, taxonomy_id=args.category, rng=rng)

        by_category = Counter(e.taxonomy_id for e in events)
        by_recoverable = Counter(e.ground_truth_recoverable for e in events)
        print(f"Injected {len(events)} failure_events across {len(mandates)} mandates.")
        print("\nBy category:")
        for cat_id in sorted(by_category, key=lambda c: int(c[1:])):
            print(f"  {cat_id}: {by_category[cat_id]}")
        print("\nBy ground_truth_recoverable:")
        for label in (True, False, None):
            print(f"  {label}: {by_recoverable.get(label, 0)}")

        orphaned = [e for e in events if e.mandate_id is None]
        assert not orphaned, "found events with no mandate_id -- FK not set correctly"
    finally:
        db.close()


if __name__ == "__main__":
    main()
