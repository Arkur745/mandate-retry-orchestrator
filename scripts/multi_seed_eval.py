"""Multi-seed evaluation: re-run simulate -> classify -> plan -> (fallback
if escalated) across N different random seeds, in N isolated databases,
and report variance on the key numbers rather than a single run's point
estimate. This is the difference between "here's a number" and "here's a
number I know is stable."

    venv/Scripts/python.exe scripts/multi_seed_eval.py
    venv/Scripts/python.exe scripts/multi_seed_eval.py --seeds 1 2 3 4 5 6 7 --mandate-count 40 --event-count 100

Deliberately skips the execute stage. classifier accuracy, escalation-
type distribution, and simulated recovered revenue -- the three metrics
this script reports -- are fully determined by simulate -> classify ->
plan -> fallback: simulated_recovered_revenue is a model-internal
estimate computed from the planner's own step probabilities, not from
what the stub executor actually returns (which always "succeeds"
unconditionally). Running the real APScheduler-backed executor per seed
would add real wall-clock time (even fast-forwarded) without changing
any number reported here, so it's left out of this script specifically.
scripts/run_pipeline.py remains the one that exercises execute end to end.

Each seed gets its own fresh SQLite file in a temp directory (not the
project's orchestrator.db) so runs never mix -- app/eval.py's report
functions operate over "the whole DB" with no run/seed scoping column,
so isolation has to happen at the database-file level.
"""
import argparse
import random
import statistics
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.classifier import classify_failure
from app.eval import (
    classifier_accuracy_report,
    escalation_type_distribution,
    simulated_recovered_revenue,
)
from app.fallback import generate_fallback_message
from app.models import Base, Mandate, PlanDecision
from app.planner import plan_retries
from app.simulator import inject_batch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_mandates import build_mandate  # noqa: E402


def run_one_seed(seed: int, mandate_count: int, event_count: int, db_path: Path) -> dict:
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    rng = random.Random(seed)
    mandates = [build_mandate(i, rng) for i in range(1, mandate_count + 1)]
    db.add_all(mandates)
    db.commit()
    for m in mandates:
        db.refresh(m)

    events = inject_batch(db, mandates, event_count, rng=rng)

    for event in events:
        mandate = db.get(Mandate, event.mandate_id)
        cls = classify_failure(db, event, mandate)
        plan = plan_retries(db, event, cls, mandate)
        if plan.decision == PlanDecision.escalate:
            generate_fallback_message(db, plan, event, mandate)

    acc = classifier_accuracy_report(db)
    dist = escalation_type_distribution(db)
    revenue = simulated_recovered_revenue(db)
    escalation_total = sum(dist.values())

    result = {
        "seed": seed,
        "aggregate_accuracy": acc.aggregate_accuracy,
        "total_scored": acc.total_scored,
        "escalation_distribution": dist,
        "escalation_total": escalation_total,
        "escalation_fractions": {
            k: (v / escalation_total if escalation_total else None) for k, v in dist.items()
        },
        "recovered_revenue_inr": revenue.total_inr,
        "retry_plan_count": revenue.retry_plan_count,
    }
    db.close()
    engine.dispose()  # release the sqlite file handle so Windows can delete the temp dir
    return result


def _mean_stdev_range(values: list[float]) -> str:
    if len(values) < 2:
        return f"{values[0]:.4f} (only one value, no variance)" if values else "n/a"
    return (
        f"mean={statistics.mean(values):.4f} stdev={statistics.stdev(values):.4f} "
        f"range=[{min(values):.4f}, {max(values):.4f}]"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3, 4, 5])
    parser.add_argument("--mandate-count", type=int, default=25)
    parser.add_argument("--event-count", type=int, default=70)
    args = parser.parse_args()

    results = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for seed in args.seeds:
            db_path = Path(tmpdir) / f"seed_{seed}.db"
            print(f"--- seed {seed} ({args.mandate_count} mandates, {args.event_count} events) ---")
            result = run_one_seed(seed, args.mandate_count, args.event_count, db_path)
            results.append(result)
            acc_str = f"{result['aggregate_accuracy']:.3f}" if result["aggregate_accuracy"] is not None else "n/a"
            print(
                f"  accuracy={acc_str} ({result['total_scored']} scored)  "
                f"escalations={result['escalation_total']}  "
                f"retry_plans={result['retry_plan_count']}  "
                f"revenue=INR {result['recovered_revenue_inr']:.2f}"
            )

    print("\n" + "=" * 70)
    print("MULTI-SEED SUMMARY")
    print("=" * 70)
    print(f"Seeds: {args.seeds}\n")

    accuracies = [r["aggregate_accuracy"] for r in results if r["aggregate_accuracy"] is not None]
    print(f"Classifier accuracy (aggregate): {_mean_stdev_range(accuracies)}")

    revenues = [r["recovered_revenue_inr"] for r in results]
    print(f"Simulated recovered revenue (INR): {_mean_stdev_range(revenues)}")

    retry_counts = [float(r["retry_plan_count"]) for r in results]
    print(f"Retry plan count: {_mean_stdev_range(retry_counts)}")

    escalation_totals = [float(r["escalation_total"]) for r in results]
    print(f"Escalation count: {_mean_stdev_range(escalation_totals)}")

    all_types = set()
    for r in results:
        all_types.update(r["escalation_distribution"].keys())

    print("\nEscalation-type fraction (of that seed's total escalations), by type:")
    for etype in sorted(all_types):
        fracs = [
            r["escalation_fractions"][etype]
            for r in results
            if r["escalation_fractions"].get(etype) is not None
        ]
        if fracs:
            print(f"  {etype}: {_mean_stdev_range(fracs)}")
        else:
            print(f"  {etype}: no escalations of this type in any seed")


if __name__ == "__main__":
    main()
