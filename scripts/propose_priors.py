"""Propose new priors: runs the same calibration as
scripts/calibration_report.py, then for every category that BOTH cleared
the sample-size threshold AND shows a delta worth acting on
(app.calibration.DELTA_THRESHOLD), fits a new base_p/decay from the
empirical data and writes them to app/priors_proposed.json.

    venv/Scripts/python.exe scripts/propose_priors.py
    venv/Scripts/python.exe scripts/propose_priors.py --db-path calibration_demo.db

This file is NEVER read automatically by the live planner -- see
app/planner.py's _load_active_overrides, which only ever reads
app/priors_active.json, a different file this script does not write.
scripts/adopt_priors.py is the only thing that turns a proposal here into
something the planner actually uses, and only for categories explicitly
named on its command line.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.calibration import (
    DELTA_THRESHOLD,
    PRIORS_PROPOSED_PATH,
    compute_calibration,
    propose_new_priors,
    proposals_to_json,
)
from app.db import SessionLocal


def _db_session(db_path: str | None):
    if db_path is None:
        return SessionLocal()
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    return sessionmaker(bind=engine)()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--out", default=str(PRIORS_PROPOSED_PATH))
    args = parser.parse_args()

    db = _db_session(args.db_path)
    try:
        report = compute_calibration(db)
    finally:
        db.close()

    proposals = propose_new_priors(report)

    print("=" * 78)
    print("PROPOSED PRIOR CHANGES -- derived from this system's own simulator.")
    print("Never read automatically by the live planner. See docs/calibration.md.")
    print("=" * 78)

    if not proposals:
        print(
            f"\nNo category cleared both the sample-size threshold and the "
            f"delta-worth-acting-on threshold ({DELTA_THRESHOLD:.2f}). Nothing proposed."
        )
    else:
        print()
        header = f"{'Category':8} {'old base_p':>11} {'new base_p':>11} {'old decay':>10} {'new decay':>15}"
        print(header)
        print("-" * len(header))
        for tid, p in sorted(proposals.items()):
            new_decay_str = f"{p.new_decay:.3f}" if p.new_decay is not None else f"{p.old_decay:.3f} (unchanged)"
            print(f"{tid:8} {p.old_base_p:>11.3f} {p.new_base_p:>11.3f} {p.old_decay:>10.3f} {new_decay_str:>15}")
            print(f"         {p.fit_note}")
            print(f"         sample sizes used: {p.basis_sample_sizes}")

    out_path = Path(args.out)
    out_path.write_text(json.dumps(proposals_to_json(proposals), indent=2), encoding="utf-8")
    print(f"\nWrote {len(proposals)} proposal(s) to {out_path}")
    if proposals:
        print(
            "To adopt any of these into the live planner: "
            f"venv/Scripts/python.exe scripts/adopt_priors.py --categories "
            f"{' '.join(sorted(proposals))}"
        )


if __name__ == "__main__":
    main()
