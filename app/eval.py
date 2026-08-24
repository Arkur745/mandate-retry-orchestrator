"""Eval harness: computation functions behind scripts/eval_report.py.

Kept separate from the report renderer so the numbers are queryable/
testable independent of markdown formatting. Reads the DB as-built by the
other pipeline stages; touches nothing (no writes) -- this module only
ever runs SELECT queries.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import (
    Classification,
    EscalationType,
    FailureEvent,
    FallbackMessage,
    Mandate,
    PlanDecision,
    RetryPlan,
)

ALL_TAXONOMY_IDS = [f"P{i}" for i in range(1, 13)]


# ---------------------------------------------------------------------
# Classifier accuracy
# ---------------------------------------------------------------------


@dataclass
class CategoryAccuracy:
    taxonomy_id: str
    scored: int  # events where both ground_truth and classifier recoverable are non-null
    correct: int
    unscored_ambiguous: int  # ground_truth or classifier recoverable is null (P12/fallback) -- can't score

    @property
    def accuracy(self) -> float | None:
        return self.correct / self.scored if self.scored else None


@dataclass
class ClassifierAccuracyReport:
    per_category: dict[str, CategoryAccuracy]
    total_scored: int
    total_correct: int

    @property
    def aggregate_accuracy(self) -> float | None:
        return self.total_correct / self.total_scored if self.total_scored else None


def classifier_accuracy_report(db: Session) -> ClassifierAccuracyReport:
    per_category: dict[str, CategoryAccuracy] = {}
    total_scored = 0
    total_correct = 0

    for taxonomy_id in ALL_TAXONOMY_IDS:
        rows = (
            db.query(FailureEvent, Classification)
            .join(Classification, Classification.failure_event_id == FailureEvent.id)
            .filter(FailureEvent.taxonomy_id == taxonomy_id)
            .all()
        )
        scored = 0
        correct = 0
        unscored = 0
        for event, cls in rows:
            if event.ground_truth_recoverable is None or cls.recoverable is None:
                unscored += 1
                continue
            scored += 1
            if event.ground_truth_recoverable == cls.recoverable:
                correct += 1
        per_category[taxonomy_id] = CategoryAccuracy(taxonomy_id, scored, correct, unscored)
        total_scored += scored
        total_correct += correct

    return ClassifierAccuracyReport(per_category, total_scored, total_correct)


# ---------------------------------------------------------------------
# Escalation-type distribution
# ---------------------------------------------------------------------


def escalation_type_distribution(db: Session) -> dict[str, int]:
    counts: dict[str, int] = {t.value: 0 for t in EscalationType}
    for (escalation_type,) in db.query(FallbackMessage.escalation_type).all():
        counts[escalation_type.value] += 1
    return counts


# ---------------------------------------------------------------------
# Simulated recovered revenue
#
# IMPORTANT (state this in the report, not just here): this is an
# EXPECTED value computed under the planner's own hand-specified
# probability model (app/planner.py's TimingProfile base_probability /
# decay_per_step numbers) -- it is NOT an empirical or real-world
# recovery-rate claim. No real debits were attempted for synthetic
# mandates; "recovered" here means "the planner's own model assigns this
# much probability mass to at least one attempt in the sequence
# succeeding", nothing more.
#
# Per-mandate P(recovered) = 1 - product(1 - p_i) over the plan's steps --
# the standard "at least one success" treatment. This is deliberately
# NOT the same formula as the planner's own search-time EV (which sums
# p_i*amount - cost_i additively, a search heuristic, not a true
# probability of recovery) -- using the additive version here would
# double-count revenue across multi-step plans.
# ---------------------------------------------------------------------


@dataclass
class RecoveredRevenueReport:
    per_category: dict[str, float] = field(default_factory=dict)
    total_paise: float = 0.0
    retry_plan_count: int = 0

    @property
    def total_inr(self) -> float:
        return self.total_paise / 100


def simulated_recovered_revenue(db: Session) -> RecoveredRevenueReport:
    report = RecoveredRevenueReport()
    plans = (
        db.query(RetryPlan)
        .filter(RetryPlan.decision == PlanDecision.retry)
        .all()
    )
    for plan in plans:
        event = db.get(FailureEvent, plan.failure_event_id)
        mandate = db.get(Mandate, event.mandate_id)
        p_none_succeed = 1.0
        for step in plan.steps:
            p_none_succeed *= 1.0 - step.success_probability
        p_recovered = 1.0 - p_none_succeed
        estimate = p_recovered * mandate.amount

        report.total_paise += estimate
        report.retry_plan_count += 1
        report.per_category[event.taxonomy_id] = report.per_category.get(event.taxonomy_id, 0.0) + estimate

    return report


# ---------------------------------------------------------------------
# S1-S9 checklist
#
# Each entry maps a taxonomy_id to the pytest node ID(s) that actually
# exercise its failure condition (not just "a test exists in that file").
# This mapping was built by hand-auditing every test file (see commit
# message / eval_audit.md) -- there is no automatic tagging convention in
# this test suite, so this list is the source of truth for "which test
# proves which S-code." What IS automatic: the checklist re-runs each
# mapped test fresh via pytest and reports real pass/fail, rather than
# trusting a stale "yes it passes" claim.
# ---------------------------------------------------------------------

S_CHECKLIST_DEFINITIONS: dict[str, dict] = {
    "S1": {
        "description": "LLM classifier returns malformed/unparseable output",
        "tests": [
            "tests/test_classifier.py::test_malformed_llm_output_triggers_s1_fallback",
            "tests/test_classifier.py::test_llm_api_exception_also_triggers_fallback_not_a_crash",
        ],
        "note": None,
    },
    "S2": {
        "description": "LLM classifier is confidently wrong (e.g. mislabels a hard-fail as retryable)",
        "tests": [
            "tests/test_constraints.py::test_upi_attempt_4_allowed_attempt_5_vetoed",
            "tests/test_constraints.py::test_generic_rail_attempt_3_allowed_attempt_4_vetoed",
        ],
        "note": (
            "The literal scenario (feed a known P4 case, check the LLM doesn't mislabel it) "
            "cannot occur in this architecture: P4/P5/P6/P8/P11 are always rule-classified, "
            "never reach the LLM path (only P3/P12 do), so the LLM cannot mislabel a hard-fail "
            "case by construction. The REQUIRED mitigation -- a hard attempt-count ceiling the "
            "classifier cannot override -- is real and tested, but independently of any LLM "
            "output (constraints.check_retry doesn't read classification at all). Flagged as a "
            "scope nuance, not a gap: the mapped tests prove the ceiling holds unconditionally."
        ),
    },
    "S3": {
        "description": "Retry storm / duplicate retry scheduling",
        "tests": ["tests/test_executor.py::test_double_scheduling_is_a_noop_not_a_duplicate_or_crash"],
        "note": None,
    },
    "S4": {
        "description": "Double-charge race condition",
        "tests": ["tests/test_executor.py::test_concurrent_claim_only_one_execution_wins"],
        "note": None,
    },
    "S5": {
        "description": "Razorpay API unavailable or rate-limited",
        "tests": [
            "tests/test_executor.py::test_backoff_retries_then_succeeds",
            "tests/test_executor.py::test_backoff_gives_up_cleanly_after_cap_without_crashing",
            "tests/test_executor.py::test_claim_and_execute_with_mocked_real_api_backoff_then_success",
            "tests/test_executor.py::test_claim_and_execute_fails_cleanly_when_real_api_exhausts_retries",
        ],
        "note": None,
    },
    "S6": {
        "description": "Constraint store and planner disagree (planner proposes a retry that violates a constraint)",
        "tests": [
            "tests/test_planner.py::TestAllVetoedEscalates::test_mandate_expiring_imminently_escalates_not_crashes",
            "tests/test_planner.py::TestAllVetoedEscalates::test_p9_on_card_emandate_is_structurally_infeasible_and_escalates",
        ],
        "note": (
            "Implementation checks each candidate DURING search (app.planner._search_candidates "
            "calls constraints.check_retry per step and excludes vetoed candidates), not as a "
            "separate pass after a complete plan is assembled. Functionally equivalent guarantee "
            "(no invalid plan is ever produced -- the mapped tests prove candidates that would "
            "otherwise be chosen get excluded and the plan honestly escalates), but architecturally "
            "different from a literal 'plan first, veto after' pipeline described in the taxonomy doc."
        ),
    },
    "S7": {
        "description": "Retry planner produces a low-value/negative-expected-value plan",
        "tests": [],
        "note": (
            "GAP: the code path exists (app.planner.EV_ESCALATE_THRESHOLD_PAISE check, "
            "'best.expected_value <= EV_ESCALATE_THRESHOLD_PAISE' branch in plan_retries) but no "
            "test in the suite drives a scenario to that specific branch -- every existing escalate "
            "test hits not_recoverable, low_confidence, or all_candidates_vetoed instead. Flagging "
            "per Day 8 scope (not fixing); a Day 9 candidate."
        ),
    },
    "S8": {
        "description": "Fallback agent generates an inappropriate customer-facing message",
        "tests": [
            "tests/test_fallback.py::test_malformed_or_malshaped_llm_output_falls_back_to_safe_default",
            "tests/test_fallback.py::test_ungrounded_content_referencing_wrong_category_falls_back_to_safe_default",
        ],
        "note": None,
    },
    "S9": {
        "description": "Audit log write fails mid-pipeline",
        "tests": [],
        "note": (
            "GAP: no test simulates a DB write failure during an audit_log insert. Architecturally, "
            "most audit_log writes share a single commit with the primary state change they "
            "describe (e.g. app.executor.claim_and_execute commits the retry_attempts outcome "
            "change and its audit_log row together), which incidentally prevents a state change "
            "from persisting without its audit trail -- but this is implicit atomicity, not an "
            "explicit 'retry the write or fail loudly' mechanism, and it is unverified by any test. "
            "Flagging per Day 8 scope (not fixing); a Day 9 candidate."
        ),
    },
}


@dataclass
class SCodeResult:
    code: str
    description: str
    test_node_ids: list[str]
    note: str | None
    status: str  # "passing", "no_tests", "failing"
    pytest_output: str


def run_s_checklist(repo_root: Path | None = None) -> list[SCodeResult]:
    """Actually re-runs each mapped test via pytest (not a cached/assumed
    result) and reports real pass/fail."""
    repo_root = repo_root or Path(__file__).resolve().parent.parent
    results: list[SCodeResult] = []

    for code, defn in S_CHECKLIST_DEFINITIONS.items():
        node_ids = defn["tests"]
        if not node_ids:
            results.append(
                SCodeResult(code, defn["description"], [], defn["note"], "no_tests", "")
            )
            continue

        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *node_ids, "-q"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        status = "passing" if proc.returncode == 0 else "failing"
        results.append(
            SCodeResult(code, defn["description"], node_ids, defn["note"], status, proc.stdout[-1000:])
        )

    return results
