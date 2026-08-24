"""Tests for app/calibration.py (compute_calibration, propose_new_priors,
adopt_priors) and the override plumbing in app/planner.py
(_effective_profile / priors_active.json).

Fixtures build retry_attempts data directly via the ORM (same convention
as tests/test_trace.py) rather than running a full simulate/classify/plan
batch -- calibration only ever reads FailureEvent + RetryAttempt +
Mandate, so that's all these tests construct.
"""
import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.calibration import (
    DELTA_THRESHOLD,
    adopt_priors,
    compute_calibration,
    propose_new_priors,
)
from app.models import Base, FailureEvent, Mandate, MandateStatus, Rail, RetryAttempt, RetryOutcome
from app.planner import CATEGORY_PROFILES, _effective_profile, _load_active_overrides

OCCURRED_AT = datetime(2026, 8, 24, 9, 0, 0)


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()


def make_mandate(db: Session, rail: Rail) -> Mandate:
    m = Mandate(
        customer_ref="cust_cal",
        rail=rail,
        amount=99900,
        status=MandateStatus.active,
        mandate_expiry=datetime(2027, 1, 1),
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def seed_step_samples(
    db: Session, taxonomy_id: str, rail: Rail, step_index: int, successes: int, total: int, prefix: str
) -> None:
    """Creates `total` independent mandates/failure_events, each
    contributing exactly one executed retry_attempts row at
    attempt_number = step_index + 2 -- mirrors how real data looks (one
    row per real (mandate, failure_event, attempt) triple; you can't have
    two retry_attempts rows sharing a failure_event+attempt_number)."""
    attempt_number = step_index + 2
    for i in range(total):
        mandate = make_mandate(db, rail)
        event = FailureEvent(
            mandate_id=mandate.id,
            taxonomy_id=taxonomy_id,
            raw_reason_text="synthetic",
            ground_truth_recoverable=True,
            occurred_at=OCCURRED_AT,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        outcome = RetryOutcome.success if i < successes else RetryOutcome.failed
        db.add(
            RetryAttempt(
                failure_event_id=event.id,
                attempt_number=attempt_number,
                scheduled_at=OCCURRED_AT,
                executed_at=OCCURRED_AT,
                outcome=outcome,
                idempotency_key=f"{prefix}-{i}",
            )
        )
    db.commit()


# ---- compute_calibration ----------------------------------------------


def test_flags_deliberately_skewed_category_but_not_its_profile_sibling(db: Session):
    # P2 and P9 SHARE one TimingProfile object (_FAST_TECHNICAL,
    # base_p=0.60) -- seed P2 close to its prior (small, expected delta)
    # and P9 deliberately far from it, and confirm calibration tells them
    # apart per-taxonomy_id despite the shared profile.
    profile = CATEGORY_PROFILES["P2"]
    assert CATEGORY_PROFILES["P9"] is profile  # sanity: this test's premise

    seed_step_samples(db, "P2", Rail.upi_autopay, step_index=0, successes=18, total=30, prefix="p2")  # 0.60, matches
    seed_step_samples(db, "P9", Rail.card_emandate, step_index=0, successes=6, total=30, prefix="p9")  # 0.20, skewed

    report = compute_calibration(db)

    p2_step0 = report.categories["P2"].steps[0]
    p9_step0 = report.categories["P9"].steps[0]

    assert p2_step0.meets_threshold and p9_step0.meets_threshold
    assert p2_step0.empirical_p == pytest.approx(0.60)
    assert p9_step0.empirical_p == pytest.approx(0.20)
    assert abs(p2_step0.delta) < DELTA_THRESHOLD  # close to its 0.60 prior -- not drift
    assert abs(p9_step0.delta) >= DELTA_THRESHOLD  # 0.20 vs 0.60 prior -- real drift

    proposals = propose_new_priors(report)
    assert "P9" in proposals
    assert "P2" not in proposals  # within threshold -- not proposed, not churn


def test_category_below_sample_threshold_excluded_not_reported_with_false_confidence(db: Session):
    # Only 5 samples, deliberately far from the 0.65 prior (would clearly
    # show as "drift" if trusted) -- must NOT be reported or proposed.
    seed_step_samples(db, "P10", Rail.e_nach, step_index=0, successes=0, total=5, prefix="p10")

    report = compute_calibration(db)
    step0 = report.categories["P10"].steps[0]

    assert step0.sample_size == 5
    assert not step0.meets_threshold
    assert step0.empirical_p == pytest.approx(0.0)  # the raw number IS computable...

    proposals = propose_new_priors(report)
    assert "P10" not in proposals  # ...but never surfaces as a proposal


def test_category_with_zero_samples_reports_cleanly(db: Session):
    report = compute_calibration(db)
    cat = report.categories["P1"]
    assert all(s.sample_size == 0 and not s.meets_threshold and s.empirical_p is None for s in cat.steps)
    assert cat.any_step_reportable is False


def test_rail_breakdown_present_for_multi_rail_category(db: Session):
    # P2 applies to all three rails -- seed two of them distinctly.
    seed_step_samples(db, "P2", Rail.upi_autopay, step_index=0, successes=25, total=25, prefix="upi")
    seed_step_samples(db, "P2", Rail.e_nach, step_index=0, successes=0, total=25, prefix="enach")

    report = compute_calibration(db)
    rails = {r.rail: r for r in report.categories["P2"].rail_breakdown}
    assert rails["upi_autopay"].empirical_p == pytest.approx(1.0)
    assert rails["e_nach"].empirical_p == pytest.approx(0.0)
    assert rails["card_emandate"].sample_size == 0


# ---- propose_new_priors: numeric fit -----------------------------------


def test_propose_new_priors_fits_base_p_and_decay_from_two_steps(db: Session):
    # fast_technical prior: base_p=0.60, decay=0.65. Seed step0 at 0.44
    # and step1 at 0.22 (both n=50, well over threshold, and both deltas
    # comfortably clear of the 0.10 threshold -- not boundary values) and
    # hand-verify the geometric-mean decay fit.
    seed_step_samples(db, "P2", Rail.upi_autopay, step_index=0, successes=22, total=50, prefix="s0")  # 0.44
    seed_step_samples(db, "P2", Rail.card_emandate, step_index=1, successes=11, total=50, prefix="s1")  # 0.22

    report = compute_calibration(db)
    proposals = propose_new_priors(report)

    assert "P2" in proposals
    p = proposals["P2"]
    assert p.new_base_p == pytest.approx(0.44)
    # decay = (0.22 / 0.44) ** (1 / (1 - 0)) = 0.5
    assert p.new_decay == pytest.approx(0.5)
    assert p.basis_sample_sizes == {0: 50, 1: 50}


def test_propose_new_priors_leaves_decay_unchanged_with_only_one_usable_step(db: Session):
    seed_step_samples(db, "P1", Rail.upi_autopay, step_index=0, successes=5, total=25, prefix="only0")  # 0.20 vs 0.40 prior

    report = compute_calibration(db)
    proposals = propose_new_priors(report)

    assert "P1" in proposals
    p = proposals["P1"]
    assert p.new_base_p == pytest.approx(0.20)
    assert p.new_decay is None
    assert p.effective_new_decay == p.old_decay
    assert "decay left unchanged" in p.fit_note


# ---- adopt_priors: scoped writes + audit log ---------------------------


def _fake_proposed(categories: dict) -> dict:
    """Builds the same on-disk shape proposals_to_json produces, without
    needing a full ProposedPrior/calibration run -- these adopt_priors
    tests are about write-scoping and the audit trail, not the fit math
    (covered separately above)."""
    return {
        "generated_at": "2026-08-24T00:00:00+00:00",
        "source": "test",
        "note": "test fixture",
        "categories": categories,
    }


def test_adopt_priors_only_changes_named_categories_others_byte_identical(db: Session, tmp_path):
    active_path = tmp_path / "priors_active.json"
    # Pre-existing adopted state for BOTH P2 and P9, as if a previous run
    # had already adopted them.
    pre_existing = {
        "P2": {"base_p": 0.55, "decay": 0.60},
        "P9": {"base_p": 0.42, "decay": 0.58},
    }
    active_path.write_text(json.dumps(pre_existing, indent=2, sort_keys=True), encoding="utf-8")

    proposed = _fake_proposed(
        {
            "P2": {
                "profile_name": "fast_technical",
                "old_base_p": 0.60, "old_decay": 0.65,
                "new_base_p": 0.51, "new_decay": 0.70,
                "fit_note": "test", "basis_sample_sizes": {0: 30, 1: 30},
            },
            "P9": {
                "profile_name": "fast_technical",
                "old_base_p": 0.60, "old_decay": 0.65,
                "new_base_p": 0.20, "new_decay": None,
                "fit_note": "test", "basis_sample_sizes": {0: 30},
            },
        }
    )

    adopt_priors(db, ["P2"], proposed, active_path=active_path)

    result = json.loads(active_path.read_text(encoding="utf-8"))
    assert result["P2"] == {"base_p": 0.51, "decay": 0.70}
    # P9 untouched -- byte-identical to what was there before this run.
    assert result["P9"] == pre_existing["P9"]


def test_adopt_priors_rejects_category_not_in_proposed_file(db: Session, tmp_path):
    active_path = tmp_path / "priors_active.json"
    proposed = _fake_proposed(
        {"P2": {"profile_name": "fast_technical", "old_base_p": 0.6, "old_decay": 0.65,
                "new_base_p": 0.5, "new_decay": 0.6, "fit_note": "t", "basis_sample_sizes": {}}}
    )
    with pytest.raises(ValueError, match="P9"):
        adopt_priors(db, ["P9"], proposed, active_path=active_path)
    assert not active_path.exists()


def test_adopt_priors_audit_log_contains_before_after(db: Session, tmp_path):
    active_path = tmp_path / "priors_active.json"
    proposed = _fake_proposed(
        {
            "P2": {
                "profile_name": "fast_technical",
                "old_base_p": 0.60, "old_decay": 0.65,
                "new_base_p": 0.51, "new_decay": 0.72,
                "fit_note": "fit from real data", "basis_sample_sizes": {0: 40, 1: 40},
            }
        }
    )

    changes = adopt_priors(db, ["P2"], proposed, active_path=active_path)
    assert changes["P2"]["before"] == {"base_p": CATEGORY_PROFILES["P2"].base_probability, "decay": CATEGORY_PROFILES["P2"].decay_per_step}
    assert changes["P2"]["after"] == {"base_p": 0.51, "decay": 0.72}

    from app.models import AuditLog

    row = db.query(AuditLog).filter_by(event_type="priors_adopted").one()
    assert row.related_entity_type == "planner_priors"
    assert row.detail["categories"] == ["P2"]
    logged = row.detail["changes"]["P2"]
    assert logged["before"]["base_p"] == pytest.approx(CATEGORY_PROFILES["P2"].base_probability)
    assert logged["after"]["base_p"] == pytest.approx(0.51)
    assert logged["after"]["decay"] == pytest.approx(0.72)
    assert "deliberate" in row.detail["note"].lower()


# ---- planner override plumbing -----------------------------------------


def test_effective_profile_unaffected_when_no_override_file(monkeypatch, tmp_path):
    monkeypatch.setattr("app.planner.PRIORS_ACTIVE_PATH", tmp_path / "does_not_exist.json")
    profile = _effective_profile("P2")
    assert profile.base_probability == CATEGORY_PROFILES["P2"].base_probability
    assert profile.decay_per_step == CATEGORY_PROFILES["P2"].decay_per_step


def test_effective_profile_picks_up_adopted_value_without_touching_profile_sibling(monkeypatch, tmp_path):
    active_path = tmp_path / "priors_active.json"
    active_path.write_text(json.dumps({"P2": {"base_p": 0.51, "decay": 0.70}}), encoding="utf-8")
    monkeypatch.setattr("app.planner.PRIORS_ACTIVE_PATH", active_path)

    p2 = _effective_profile("P2")
    p9 = _effective_profile("P9")  # shares the same underlying TimingProfile object as P2

    assert p2.base_probability == pytest.approx(0.51)
    assert p2.decay_per_step == pytest.approx(0.70)
    # P9 must be completely unaffected -- confirms _effective_profile
    # never mutates the shared module-level TimingProfile object.
    assert p9.base_probability == CATEGORY_PROFILES["P9"].base_probability
    assert p9.decay_per_step == CATEGORY_PROFILES["P9"].decay_per_step
    assert CATEGORY_PROFILES["P2"].base_probability != 0.51  # original untouched too


def test_load_active_overrides_tolerates_missing_and_malformed_file(monkeypatch, tmp_path):
    missing = tmp_path / "missing.json"
    monkeypatch.setattr("app.planner.PRIORS_ACTIVE_PATH", missing)
    assert _load_active_overrides() == {}

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr("app.planner.PRIORS_ACTIVE_PATH", malformed)
    assert _load_active_overrides() == {}  # never crashes plan_retries over a bad artifact
