"""Calibration report: for each taxonomy category with enough executed
retry_attempts to be meaningful, compares the planner's hand-specified
TimingProfile probabilities (app/planner.py) against the empirical
per-step success rate actually observed in retry_attempts.outcome.

    venv/Scripts/python.exe scripts/calibration_report.py
    venv/Scripts/python.exe scripts/calibration_report.py --db-path calibration_demo.db
    venv/Scripts/python.exe scripts/calibration_report.py --json-out calibration_report.json

Read-only: this script never writes anything to the database or to any
priors file. See scripts/propose_priors.py for the next (also read-only,
still not planner-affecting) step, and docs/calibration.md for the full
three-step design.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.calibration import CalibrationReport, compute_calibration
from app.db import SessionLocal

CAVEAT = """
================================================================================
CAVEAT -- read this before trusting any number below.

This calibration is computed entirely from THIS SYSTEM'S OWN failure
simulator (app/simulator.py) and stub/test-mode executor (app/executor.py),
never from real Razorpay-side payment outcomes. A close match between the
empirical rate and the hand-specified prior demonstrates that the
CALIBRATION MECHANISM works end to end -- real data in, correctly
thresholded and flagged numbers out. It does NOT demonstrate that the
original hand-specified priors are accurate against real-world outcomes.
See docs/calibration.md for what would need to be true before this
mechanism could be trusted to run against real data.
================================================================================
""".strip("\n")


def _db_session(db_path: str | None):
    if db_path is None:
        return SessionLocal()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine)()


def format_report(report: CalibrationReport) -> str:
    lines = [CAVEAT, ""]
    lines.append(f"Generated: {report.generated_at.isoformat()}")
    lines.append(f"Minimum executed-attempt sample size to report a step: {report.min_sample_size}")
    lines.append(
        f"Delta threshold flagged as DRIFT (also propose_priors.py's 'worth acting "
        f"on' threshold): {report.delta_threshold:.2f}"
    )
    lines.append("")

    any_reportable = False
    for taxonomy_id, cat in report.categories.items():
        lines.append(
            f"[{taxonomy_id}] profile={cat.profile_name}  "
            f"hand-specified: base_p={cat.hand_specified_base_p:.3f} decay={cat.hand_specified_decay:.3f}"
        )
        for step in cat.steps:
            if step.meets_threshold:
                any_reportable = True
                flag = "DRIFT" if abs(step.delta) >= report.delta_threshold else "ok"
                lines.append(
                    f"    step {step.step_index}: predicted={step.predicted_p:.3f} "
                    f"empirical={step.empirical_p:.3f} (n={step.sample_size})  "
                    f"delta={step.delta:+.3f}  [{flag}]"
                )
            else:
                lines.append(
                    f"    step {step.step_index}: predicted={step.predicted_p:.3f}  "
                    f"below sample threshold (n={step.sample_size}/{report.min_sample_size}) -- not reported"
                )

        reportable_rails = [r for r in cat.rail_breakdown if r.meets_threshold]
        if len(cat.rail_breakdown) > 1:
            lines.append("    per-rail (all steps combined; diagnostic only, not used for proposals):")
            if reportable_rails:
                for r in cat.rail_breakdown:
                    if r.meets_threshold:
                        lines.append(f"      {r.rail:16} empirical_p={r.empirical_p:.3f} (n={r.sample_size})")
                    else:
                        lines.append(f"      {r.rail:16} below sample threshold (n={r.sample_size})")
            else:
                lines.append("      (no rail cleared the sample threshold)")
        lines.append("")

    if not any_reportable:
        lines.append(
            "No category/step cleared the sample-size threshold -- nothing reportable "
            "yet. Run a larger batch (see scripts/seed_calibration_batch.py)."
        )

    return "\n".join(lines)


def report_to_json(report: CalibrationReport) -> dict:
    return {
        "caveat": (
            "Calibrated against this system's own simulator, not real Razorpay "
            "outcomes -- see docs/calibration.md."
        ),
        "generated_at": report.generated_at.isoformat(),
        "min_sample_size": report.min_sample_size,
        "delta_threshold": report.delta_threshold,
        "categories": {
            tid: {
                "profile_name": cat.profile_name,
                "hand_specified_base_p": cat.hand_specified_base_p,
                "hand_specified_decay": cat.hand_specified_decay,
                "steps": [
                    {
                        "step_index": s.step_index,
                        "predicted_p": s.predicted_p,
                        "empirical_p": s.empirical_p,
                        "sample_size": s.sample_size,
                        "delta": s.delta,
                        "meets_threshold": s.meets_threshold,
                    }
                    for s in cat.steps
                ],
                "rail_breakdown": [
                    {
                        "rail": r.rail,
                        "empirical_p": r.empirical_p,
                        "sample_size": r.sample_size,
                        "meets_threshold": r.meets_threshold,
                    }
                    for r in cat.rail_breakdown
                ],
            }
            for tid, cat in report.categories.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path", default=None,
        help="Path to a sqlite db file. Defaults to the app's configured database.",
    )
    parser.add_argument("--json-out", default=None, help="Optional path to also write the report as JSON.")
    args = parser.parse_args()

    db = _db_session(args.db_path)
    try:
        report = compute_calibration(db)
    finally:
        db.close()

    print(format_report(report))

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report_to_json(report), indent=2), encoding="utf-8")
        print(f"\nWrote JSON report to {args.json_out}")


if __name__ == "__main__":
    main()
