"""Adopt priors: the one deliberate, human-triggered action in this whole
mechanism that actually changes what the live planner does. Copies the
NAMED categories' values from app/priors_proposed.json (written by
scripts/propose_priors.py) into app/priors_active.json, the file
app.planner._effective_profile reads on every plan_retries call, and
commits one audit_log row recording exactly what changed and that it was
deliberate.

    venv/Scripts/python.exe scripts/adopt_priors.py --categories P2 P9

--categories is required -- there is no "adopt everything" default. Every
adoption must name exactly what it's adopting; see docs/calibration.md for
why (payments-context compliance/safety argument, not just caution for its
own sake).

Nothing else needs to be touched afterward: the next call to
app.planner.plan_retries (a new script run, a new API request, a fresh
process) re-reads app/priors_active.json and picks the new value up
immediately -- no restart, no redeploy, no other file edited.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.calibration import PRIORS_PROPOSED_PATH, adopt_priors, load_proposed_priors
from app.db import SessionLocal
from app.planner import PRIORS_ACTIVE_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--categories", nargs="+", required=True, metavar="CATEGORY",
        help="Taxonomy codes to adopt from the proposed-priors file, e.g. --categories P2 P9. "
             "No default -- every adoption must name exactly what it's adopting.",
    )
    parser.add_argument("--proposed-path", default=str(PRIORS_PROPOSED_PATH))
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation prompt.")
    args = parser.parse_args()

    proposed_path = Path(args.proposed_path)
    try:
        proposed = load_proposed_priors(proposed_path)
    except FileNotFoundError as exc:
        raise SystemExit(str(exc)) from exc

    proposed_categories = proposed.get("categories", {})
    unknown = [c for c in args.categories if c not in proposed_categories]
    if unknown:
        raise SystemExit(
            f"Not present in {proposed_path.name}'s proposals: {unknown}. "
            f"Available: {sorted(proposed_categories)}"
        )

    print(f"About to write to {PRIORS_ACTIVE_PATH}:")
    for taxonomy_id in args.categories:
        entry = proposed_categories[taxonomy_id]
        new_decay_display = entry["new_decay"] if entry["new_decay"] is not None else "(unchanged)"
        print(
            f"  {taxonomy_id}: base_p -> {entry['new_base_p']:.3f}, decay -> {new_decay_display}"
        )
        print(f"      {entry['fit_note']}")

    if not args.yes:
        confirm = input("\nProceed with this adoption? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted -- nothing written, no audit_log entry created.")
            return

    db = SessionLocal()
    try:
        changes = adopt_priors(db, args.categories, proposed, active_path=PRIORS_ACTIVE_PATH)
    finally:
        db.close()

    print(f"\nAdopted {len(changes)} categor{'y' if len(changes) == 1 else 'ies'}:")
    for taxonomy_id, change in changes.items():
        before, after = change["before"], change["after"]
        print(
            f"  {taxonomy_id}: base_p {before['base_p']:.3f} -> {after['base_p']:.3f}, "
            f"decay {before['decay']:.3f} -> {after['decay']:.3f}"
        )
    print(f"\n{PRIORS_ACTIVE_PATH} updated. One audit_log entry written "
          f"(related_entity_type='planner_priors', event_type='priors_adopted').")
    print("The planner will use these values on its very next plan_retries call.")


if __name__ == "__main__":
    main()
