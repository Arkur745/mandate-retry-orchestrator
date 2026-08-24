"""Calibration: compares the planner's hand-specified TimingProfile
probabilities (app/planner.py's base_probability/decay_per_step) against
the empirical per-step success rate actually observed in retry_attempts.

Read-only computation, like app/eval.py -- no writes, no planner changes.
Used by scripts/calibration_report.py and scripts/propose_priors.py.

IMPORTANT, and this must also appear unmissably in the report script's own
output, not just here: every outcome this module reads comes from this
system's OWN failure simulator and (stub, in the absence of a completed
real Razorpay token) executor. This validates that the calibration
mechanism works end to end -- real signal in, real numbers out, correctly
flagged and thresholded -- it does NOT validate that the original
hand-specified priors were accurate against real-world Razorpay outcomes.
See docs/calibration.md for why that gap is left open deliberately rather
than closed with a live feedback loop.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import AuditLog, FailureEvent, Mandate, RetryAttempt, RetryOutcome
from app.planner import CATEGORY_PROFILES, PRIORS_ACTIVE_PATH
from app.simulator import TAXONOMY

# Default output location for scripts/propose_priors.py -- a draft/
# candidate file, regenerable any time, gitignored (see .gitignore).
# Distinct from app.planner.PRIORS_ACTIVE_PATH, which IS the file the
# live planner reads and is not ignored.
PRIORS_PROPOSED_PATH = Path(__file__).resolve().parent / "priors_proposed.json"

# Below this many EXECUTED (success or failed -- pending/executing/skipped
# don't count) attempts at a given (category, step) pair, an empirical
# rate isn't reported at all, not even with a caveat. Rationale: for a
# true rate of 0.5, the standard error of a sample proportion at n=20 is
# ~11 percentage points, i.e. a 95%-ish interval spans roughly +/-22pp --
# already wide, but at n=2 (the explicit non-example from the task) it's
# meaningless (any single flip changes the "rate" by 50 points). 20 is a
# deliberately round, conservative floor: enough that a reported delta
# reflects a real signal rather than a coin-flip run of bad luck, not a
# claim of rigorous statistical power for a demo-scale project.
MIN_SAMPLE_SIZE = 20

# A predicted-vs-empirical gap smaller than this is within the range a
# reasonable hand-specified guess could land in it just fine -- proposing
# a change on it would be churn on noise, not a real finding. 0.10 (10
# percentage points) is deliberately coarse: this project's priors were
# explicitly scoped as "hand-specified, not fit from data" (see
# app/planner.py's module docstring); only a gap big enough that no
# reasonable hand-specification would land inside it is worth flagging as
# "the model and the data disagree," as opposed to "the model is
# approximately right."
DELTA_THRESHOLD = 0.10


def _step_index(attempt_number: int) -> int:
    """attempt_number is 1-indexed with 1 = the original failed debit
    (app.models.PlannedAttempt's own convention) -- the first RETRY is
    attempt_number=2, i.e. step_index 0 in TimingProfile terms."""
    return attempt_number - 2


@dataclass
class StepCalibration:
    taxonomy_id: str
    step_index: int
    predicted_p: float
    successes: int
    sample_size: int

    @property
    def empirical_p(self) -> float | None:
        return self.successes / self.sample_size if self.sample_size else None

    @property
    def meets_threshold(self) -> bool:
        return self.sample_size >= MIN_SAMPLE_SIZE

    @property
    def delta(self) -> float | None:
        """empirical - predicted. Only meaningful (and only ever surfaced
        in the report/proposal) when meets_threshold is True."""
        if self.empirical_p is None:
            return None
        return self.empirical_p - self.predicted_p


@dataclass
class RailBreakdown:
    taxonomy_id: str
    rail: str
    successes: int
    sample_size: int

    @property
    def empirical_p(self) -> float | None:
        return self.successes / self.sample_size if self.sample_size else None

    @property
    def meets_threshold(self) -> bool:
        return self.sample_size >= MIN_SAMPLE_SIZE


@dataclass
class CategoryCalibration:
    taxonomy_id: str
    profile_name: str
    hand_specified_base_p: float
    hand_specified_decay: float
    steps: list[StepCalibration] = field(default_factory=list)
    rail_breakdown: list[RailBreakdown] = field(default_factory=list)

    @property
    def any_step_reportable(self) -> bool:
        return any(s.meets_threshold for s in self.steps)


@dataclass
class CalibrationReport:
    min_sample_size: int
    delta_threshold: float
    generated_at: datetime
    categories: dict[str, CategoryCalibration] = field(default_factory=dict)


def compute_calibration(
    db: Session, *, min_sample_size: int = MIN_SAMPLE_SIZE
) -> CalibrationReport:
    """Reads every EXECUTED retry_attempts row (outcome in success/failed
    -- pending/executing/skipped carry no outcome signal yet) joined back
    to its failure_event's taxonomy_id and mandate's rail, and aggregates
    per (taxonomy_id, step_index) and per (taxonomy_id, rail)."""
    rows = (
        db.query(RetryAttempt.attempt_number, RetryAttempt.outcome, FailureEvent.taxonomy_id, Mandate.rail)
        .join(FailureEvent, FailureEvent.id == RetryAttempt.failure_event_id)
        .join(Mandate, Mandate.id == FailureEvent.mandate_id)
        .filter(RetryAttempt.outcome.in_([RetryOutcome.success, RetryOutcome.failed]))
        .all()
    )

    step_counts: dict[tuple[str, int], list[int]] = {}
    rail_counts: dict[tuple[str, str], list[int]] = {}

    for attempt_number, outcome, taxonomy_id, rail in rows:
        step_idx = _step_index(attempt_number)
        if step_idx < 0:
            continue  # defensive: scheduled retries are always attempt_number >= 2
        success = 1 if outcome == RetryOutcome.success else 0

        bucket = step_counts.setdefault((taxonomy_id, step_idx), [0, 0])
        bucket[0] += success
        bucket[1] += 1

        rbucket = rail_counts.setdefault((taxonomy_id, rail.value), [0, 0])
        rbucket[0] += success
        rbucket[1] += 1

    categories: dict[str, CategoryCalibration] = {}
    for taxonomy_id, profile in sorted(CATEGORY_PROFILES.items(), key=lambda kv: kv[0]):
        max_steps = len(profile.slot_offsets_hours)
        if profile.max_attempts_override is not None:
            max_steps = min(max_steps, profile.max_attempts_override)

        steps = []
        for step_idx in range(max_steps):
            predicted_p = profile.base_probability * (profile.decay_per_step**step_idx)
            successes, total = step_counts.get((taxonomy_id, step_idx), [0, 0])
            steps.append(StepCalibration(taxonomy_id, step_idx, predicted_p, successes, total))

        rails = []
        category_meta = TAXONOMY.get(taxonomy_id)
        applicable_rails = sorted(r.value for r in category_meta.rails) if category_meta else []
        for rail_value in applicable_rails:
            successes, total = rail_counts.get((taxonomy_id, rail_value), [0, 0])
            rails.append(RailBreakdown(taxonomy_id, rail_value, successes, total))

        categories[taxonomy_id] = CategoryCalibration(
            taxonomy_id=taxonomy_id,
            profile_name=profile.name,
            hand_specified_base_p=profile.base_probability,
            hand_specified_decay=profile.decay_per_step,
            steps=steps,
            rail_breakdown=rails,
        )

    return CalibrationReport(
        min_sample_size=min_sample_size,
        delta_threshold=DELTA_THRESHOLD,
        generated_at=datetime.now(timezone.utc),
        categories=categories,
    )


@dataclass
class ProposedPrior:
    taxonomy_id: str
    profile_name: str
    old_base_p: float
    old_decay: float
    new_base_p: float
    new_decay: float | None  # None means "left unchanged -- insufficient data to refit"
    fit_note: str
    basis_sample_sizes: dict[int, int] = field(default_factory=dict)

    @property
    def effective_new_decay(self) -> float:
        return self.new_decay if self.new_decay is not None else self.old_decay

    @property
    def base_p_delta(self) -> float:
        return self.new_base_p - self.old_base_p

    @property
    def decay_delta(self) -> float:
        return self.effective_new_decay - self.old_decay


def _fit_probability_model(
    steps: list[StepCalibration],
) -> tuple[float, float | None, str] | None:
    """new_base_p = empirical rate at the earliest step that cleared the
    sample threshold. new_decay = a geometric-mean ratio between the
    earliest and latest threshold-cleared steps (a closed-form generalization
    of "ratio between two adjacent steps" to however many steps actually
    have enough data), clipped to (0.05, 1.0] since decay > 1 would mean
    probability INCREASING with attempt position, which contradicts the
    model this planner uses everywhere else. Returns None if no step
    cleared the threshold at all."""
    usable = sorted((s for s in steps if s.meets_threshold), key=lambda s: s.step_index)
    if not usable:
        return None

    new_base_p = usable[0].empirical_p
    fit_note = f"base_p = empirical rate at step {usable[0].step_index} (n={usable[0].sample_size})"

    if len(usable) == 1:
        return new_base_p, None, fit_note + "; decay left unchanged (only one step cleared the sample threshold)"

    first, last = usable[0], usable[-1]
    steps_apart = last.step_index - first.step_index
    if not first.empirical_p or first.empirical_p <= 0:
        return new_base_p, None, fit_note + "; decay left unchanged (step-0 empirical rate is 0, ratio undefined)"

    ratio = last.empirical_p / first.empirical_p
    new_decay = ratio ** (1 / steps_apart)
    clipped = max(0.05, min(1.0, new_decay))
    clip_note = "" if clipped == new_decay else f" (clipped from raw fit {new_decay:.3f})"
    fit_note += (
        f"; decay fit from steps {first.step_index}->{last.step_index} "
        f"(n={first.sample_size},{last.sample_size}){clip_note}"
    )
    return new_base_p, clipped, fit_note


def propose_new_priors(
    report: CalibrationReport, *, delta_threshold: float = DELTA_THRESHOLD
) -> dict[str, ProposedPrior]:
    """Categories that both cleared the sample-size threshold (at least
    one step) AND show a delta worth acting on (base_p or effective decay
    off by at least delta_threshold). Categories with data that's simply
    consistent with the hand-specified prior are intentionally excluded --
    this is a list of proposed CHANGES, not a status report of everything
    that was measured (that's calibration_report.py's job)."""
    proposals: dict[str, ProposedPrior] = {}

    for taxonomy_id, cat in report.categories.items():
        fit = _fit_probability_model(cat.steps)
        if fit is None:
            continue
        new_base_p, new_decay, fit_note = fit

        proposal = ProposedPrior(
            taxonomy_id=taxonomy_id,
            profile_name=cat.profile_name,
            old_base_p=cat.hand_specified_base_p,
            old_decay=cat.hand_specified_decay,
            new_base_p=new_base_p,
            new_decay=new_decay,
            fit_note=fit_note,
            basis_sample_sizes={s.step_index: s.sample_size for s in cat.steps if s.meets_threshold},
        )

        if abs(proposal.base_p_delta) >= delta_threshold or abs(proposal.decay_delta) >= delta_threshold:
            proposals[taxonomy_id] = proposal

    return proposals


def proposals_to_json(proposals: dict[str, ProposedPrior], *, generated_at: datetime | None = None) -> dict:
    """The on-disk shape scripts/propose_priors.py writes to
    PRIORS_PROPOSED_PATH -- factored out here (not just in the script) so
    tests can exercise the exact same serialization adopt_priors reads."""
    return {
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "source": "scripts/propose_priors.py",
        "note": "Candidate values only. Never read automatically by the live planner. See docs/calibration.md.",
        "categories": {
            tid: {
                "profile_name": p.profile_name,
                "old_base_p": p.old_base_p,
                "old_decay": p.old_decay,
                "new_base_p": p.new_base_p,
                "new_decay": p.new_decay,
                "fit_note": p.fit_note,
                "basis_sample_sizes": p.basis_sample_sizes,
            }
            for tid, p in proposals.items()
        },
    }


def load_proposed_priors(path: Path = PRIORS_PROPOSED_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} does not exist -- run scripts/propose_priors.py first."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def adopt_priors(
    db: Session,
    categories: list[str],
    proposed: dict,
    *,
    active_path: Path = PRIORS_ACTIVE_PATH,
) -> dict[str, dict]:
    """The one write path in this whole mechanism. Deliberately requires
    an explicit `categories` list (no "adopt everything" default -- see
    docs/calibration.md) and NEVER touches a category not named here,
    whether in the on-disk active-overrides file or the audit_log entry.

    Merges into whatever's already in active_path (so a previous
    adoption of a different category isn't clobbered), writes the merged
    result, and commits exactly one audit_log row recording, per adopted
    category, the before/after base_p and decay and a note that this was
    a deliberate action, not automatic. Returns that same before/after
    mapping so a caller (CLI or test) can assert against it without
    re-reading the DB.
    """
    proposed_categories = proposed.get("categories", {})
    unknown = [c for c in categories if c not in proposed_categories]
    if unknown:
        raise ValueError(
            f"Not present in the proposed-priors file: {unknown}. "
            f"Available: {sorted(proposed_categories)}"
        )

    active: dict = {}
    if active_path.exists():
        active = json.loads(active_path.read_text(encoding="utf-8"))

    changes: dict[str, dict] = {}
    for taxonomy_id in categories:
        entry = proposed_categories[taxonomy_id]
        current_profile = CATEGORY_PROFILES[taxonomy_id]
        existing_override = active.get(taxonomy_id, {})
        before_base_p = existing_override.get("base_p", current_profile.base_probability)
        before_decay = existing_override.get("decay", current_profile.decay_per_step)
        after_base_p = entry["new_base_p"]
        after_decay = entry["new_decay"] if entry["new_decay"] is not None else before_decay

        changes[taxonomy_id] = {
            "before": {"base_p": before_base_p, "decay": before_decay},
            "after": {"base_p": after_base_p, "decay": after_decay},
            "fit_note": entry["fit_note"],
        }
        active[taxonomy_id] = {"base_p": after_base_p, "decay": after_decay}

    active_path.write_text(json.dumps(active, indent=2, sort_keys=True), encoding="utf-8")

    db.add(
        AuditLog(
            related_entity_type="planner_priors",
            related_entity_id=0,
            event_type="priors_adopted",
            detail={
                "adopted_at": datetime.now(timezone.utc).isoformat(),
                "categories": list(changes.keys()),
                "changes": changes,
                "note": (
                    "Deliberate adoption via scripts/adopt_priors.py -- not an "
                    "automatic feedback loop. See docs/calibration.md."
                ),
            },
        )
    )
    db.commit()

    return changes
