"""Generates a large batch of executed retry_attempts for
scripts/calibration_report.py to have something real to measure, in a
DEDICATED sqlite file (never orchestrator.db, the buildathon demo data).

    venv/Scripts/python.exe scripts/seed_calibration_batch.py
    venv/Scripts/python.exe scripts/seed_calibration_batch.py --db-path calibration_demo.db --mandate-count 300 --event-count 4000

Uses the REAL, unmodified app.simulator.inject_batch, app.classifier.
classify_failure, app.planner.plan_retries, and app.executor.
schedule_retry_plan -- this script does not reimplement or bypass any
classification or planning decision.

What it DOES do differently from scripts/run_pipeline.py, and why, in one
place instead of scattered comments:

1. LLM classification (P3/P12) is MOCKED, not real. Running thousands of
   real Groq calls to get calibration-scale sample sizes would be slow,
   costly, and -- as this project found more than once (see
   docs/demo_mandates.md's note on model choice) -- runs straight into the
   free-tier daily token quota. The mock's decisions are seeded and
   documented below; this is a knowingly synthetic classification signal
   for volume, not a claim about real Groq behavior.

2. retry_attempts.outcome is NOT produced by app.executor.claim_and_execute.
   claim_and_execute's stub debit call (_stub_debit_call) always returns
   "captured" -- deterministic 100% success for every synthetic mandate,
   by design (there's no real Razorpay-side signal to vary it against).
   Calibrating against a column that is always 100% success by construction
   would be a tautology, not a demonstration of the calibration mechanism.
   So THIS SCRIPT, and only this script, draws each retry_attempts row's
   outcome from an explicit synthetic "true" probability model: the
   planner's own predicted probability at that step, PLUS a small,
   documented, per-category perturbation (TRUE_RATE_PERTURBATION below) --
   enough to produce genuine, bounded, honest drift for the calibration
   report to find, without hand-fabricating a specific finding. This
   model lives ONLY here; app/executor.py is completely untouched, and the
   live pipeline (trace viewer, interactive simulator, run_pipeline.py)
   still always stub-succeeds exactly as before.
"""
import argparse
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, update
from sqlalchemy.orm import sessionmaker

from app.classifier import classify_failure
from app.executor import schedule_retry_plan
from app.models import Base, Mandate, PlanDecision, RetryAttempt, RetryOutcome
from app.planner import CATEGORY_PROFILES, plan_retries
from app.simulator import inject_batch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_mandates import build_mandate  # noqa: E402

# Deliberate, documented perturbation from each category's hand-specified
# base_probability -- NOT a claim about real-world accuracy. Just enough
# controlled "the real rate differs a bit from the guess" variance that
# calibrating against this batch is a genuine exercise (finds real deltas,
# correctly leaves most categories alone) rather than either a tautology
# (empirical always equals predicted) or a fabricated single story.
# P9 is omitted deliberately: P9/card_emandate always structurally
# escalates before ever reaching execution (the Day 5/6/9 rail-spacing
# collision, still correctly unfixed) -- it produces zero retry_attempts
# no matter how large this batch is, so a perturbation for it would never
# be exercised.
TRUE_RATE_PERTURBATION = {
    "P1": -0.06,
    "P2": +0.15,  # the one deliberately-notable drift in this batch
    "P3": 0.00,
    "P7": -0.04,
    "P10": +0.02,
    "P12": 0.00,
}


def _mock_llm_decision(rng: random.Random, taxonomy_id: str, ground_truth: bool | None) -> dict:
    """Fast, seeded stand-in for a real Groq classification -- only ever
    used for P3/P12 (classify_failure never calls Groq for anything else).
    Mirrors ground truth with a little noise for P3 (roughly matching what
    the real classifier does per docs/eval_audit.md's Day 9 Part B
    investigation); P12 gets a documented, non-zero chance of a
    'recoverable' verdict so the cautious_single profile it shares with P3
    gets SOME retry-attempt coverage, matching P12's own taxonomy framing
    as a genuine judgment call, not a guaranteed 'no'."""
    if taxonomy_id == "P3":
        recoverable = ground_truth
        if rng.random() < 0.05:
            recoverable = True if recoverable is None else not recoverable
    else:  # P12
        recoverable = True if rng.random() < 0.35 else None
    confidence = 0.85 if recoverable is not None else 0.4
    return {
        "recoverable": recoverable,
        "confidence": confidence,
        "reasoning": "synthetic calibration batch -- mocked LLM decision, not a real Groq call",
    }


def _mock_groq_client(rng: random.Random, taxonomy_id: str, ground_truth: bool | None) -> MagicMock:
    message = MagicMock()
    message.content = json.dumps(_mock_llm_decision(rng, taxonomy_id, ground_truth))
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    client = MagicMock()
    client.chat.completions.create.return_value = response
    return client


def _true_probability(taxonomy_id: str, step_index: int) -> float:
    profile = CATEGORY_PROFILES[taxonomy_id]
    predicted = profile.base_probability * (profile.decay_per_step**step_index)
    perturbed = predicted + TRUE_RATE_PERTURBATION.get(taxonomy_id, 0.0)
    return max(0.02, min(0.98, perturbed))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", default="calibration_demo.db")
    parser.add_argument("--mandate-count", type=int, default=300)
    parser.add_argument("--event-count", type=int, default=4000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    db_path = Path(args.db_path)
    if db_path.exists():
        db_path.unlink()  # this script always starts from a clean, dedicated file

    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    rng = random.Random(args.seed)

    print(f"[1/3] Seeding {args.mandate_count} mandates into {db_path} (fresh file)...")
    mandates = [build_mandate(i, rng) for i in range(1, args.mandate_count + 1)]
    db.add_all(mandates)
    db.commit()
    for m in mandates:
        db.refresh(m)

    print(f"[2/3] Injecting {args.event_count} failure events...")
    events = inject_batch(db, mandates, args.event_count, rng=rng)

    print("[3/3] Classifying, planning, and synthesizing outcomes for each event...")
    retry_count = 0
    escalate_count = 0
    attempt_count = 0
    for event in events:
        mandate = db.get(Mandate, event.mandate_id)
        if event.taxonomy_id in ("P3", "P12"):
            groq_client = _mock_groq_client(rng, event.taxonomy_id, event.ground_truth_recoverable)
            cls = classify_failure(db, event, mandate, groq_client=groq_client)
        else:
            cls = classify_failure(db, event, mandate)  # rule path, never touches Groq

        plan = plan_retries(db, event, cls, mandate)
        if plan.decision != PlanDecision.retry:
            escalate_count += 1
            continue
        retry_count += 1

        rows = schedule_retry_plan(db, plan)
        for row in rows:
            step_index = row.attempt_number - 2
            true_p = _true_probability(event.taxonomy_id, step_index)
            outcome = RetryOutcome.success if rng.random() < true_p else RetryOutcome.failed
            db.execute(
                update(RetryAttempt)
                .where(RetryAttempt.id == row.id)
                .values(outcome=outcome, executed_at=datetime.now(timezone.utc).replace(tzinfo=None))
            )
            attempt_count += 1
        db.commit()

    print(
        f"\nDone. {len(events)} events -> {retry_count} retry plans "
        f"({attempt_count} attempts synthesized), {escalate_count} escalations."
    )
    print(f"Database: {db_path}")
    print(f"Next: venv/Scripts/python.exe scripts/calibration_report.py --db-path {db_path}")

    db.close()
    engine.dispose()


if __name__ == "__main__":
    main()
