"""Tests for app/eval.py's computation functions (not run_s_checklist,
which shells out to pytest recursively -- verified manually via actual
report runs instead)."""
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.eval import (
    classifier_accuracy_report,
    escalation_type_distribution,
    simulated_recovered_revenue,
)
from app.models import (
    Base,
    Classification,
    ClassificationMethod,
    EscalationType,
    FailureEvent,
    FallbackMessage,
    FallbackMethod,
    Mandate,
    MandateStatus,
    PlanDecision,
    PlannedAttempt,
    Rail,
    RetryPlan,
)

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


def make_mandate(db: Session, rail: Rail = Rail.upi_autopay, amount: int = 100_000) -> Mandate:
    m = Mandate(
        customer_ref="c",
        rail=rail,
        amount=amount,
        status=MandateStatus.active,
        mandate_expiry=datetime(2027, 1, 1),
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def make_event_and_classification(
    db: Session, mandate: Mandate, taxonomy_id: str, *, ground_truth, classified_recoverable
) -> tuple[FailureEvent, Classification]:
    event = FailureEvent(
        mandate_id=mandate.id,
        taxonomy_id=taxonomy_id,
        raw_reason_text="x",
        ground_truth_recoverable=ground_truth,
        occurred_at=OCCURRED_AT,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    cls = Classification(
        failure_event_id=event.id,
        method=ClassificationMethod.rule,
        recoverable=classified_recoverable,
        confidence=1.0,
        reasoning="r",
    )
    db.add(cls)
    db.commit()
    db.refresh(cls)
    return event, cls


# ---- classifier_accuracy_report -------------------------------------------


def test_classifier_accuracy_counts_correct_and_incorrect(db: Session):
    mandate = make_mandate(db)
    make_event_and_classification(db, mandate, "P1", ground_truth=True, classified_recoverable=True)
    make_event_and_classification(db, mandate, "P1", ground_truth=True, classified_recoverable=True)
    make_event_and_classification(db, mandate, "P1", ground_truth=False, classified_recoverable=True)  # wrong

    report = classifier_accuracy_report(db)
    p1 = report.per_category["P1"]
    assert p1.scored == 3
    assert p1.correct == 2
    assert p1.accuracy == pytest.approx(2 / 3)
    assert report.total_scored == 3
    assert report.total_correct == 2
    assert report.aggregate_accuracy == pytest.approx(2 / 3)


def test_classifier_accuracy_excludes_null_ground_truth_or_null_recoverable(db: Session):
    mandate = make_mandate(db)
    # P12 case: ground truth is None (genuinely ambiguous in the simulator).
    make_event_and_classification(db, mandate, "P12", ground_truth=None, classified_recoverable=True)
    # Fallback case: classifier recoverable is None.
    make_event_and_classification(db, mandate, "P12", ground_truth=None, classified_recoverable=None)

    report = classifier_accuracy_report(db)
    p12 = report.per_category["P12"]
    assert p12.scored == 0
    assert p12.unscored_ambiguous == 2
    assert p12.accuracy is None


def test_classifier_accuracy_covers_all_twelve_categories_even_with_no_data(db: Session):
    report = classifier_accuracy_report(db)
    assert set(report.per_category.keys()) == {f"P{i}" for i in range(1, 13)}
    assert report.total_scored == 0
    assert report.aggregate_accuracy is None


# ---- escalation_type_distribution ------------------------------------------


def test_escalation_type_distribution_counts_each_type(db: Session):
    mandate = make_mandate(db)
    event, cls = make_event_and_classification(db, mandate, "P4", ground_truth=False, classified_recoverable=False)
    plan = RetryPlan(
        failure_event_id=event.id,
        classification_id=cls.id,
        decision=PlanDecision.escalate,
        reasoning="r",
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    db.add(
        FallbackMessage(
            retry_plan_id=plan.id,
            escalation_type=EscalationType.reauth_needed,
            template_key="reauth_needed",
            method=FallbackMethod.llm,
            content="x",
            validation_passed=True,
        )
    )
    db.add(
        FallbackMessage(
            retry_plan_id=plan.id,
            escalation_type=EscalationType.reauth_needed,
            template_key="reauth_needed",
            method=FallbackMethod.llm,
            content="y",
            validation_passed=True,
        )
    )
    db.add(
        FallbackMessage(
            retry_plan_id=plan.id,
            escalation_type=EscalationType.merchant_escalation,
            template_key="merchant_escalation",
            method=FallbackMethod.safe_default,
            content="z",
            validation_passed=False,
        )
    )
    db.commit()

    dist = escalation_type_distribution(db)
    assert dist["reauth_needed"] == 2
    assert dist["merchant_escalation"] == 1
    assert dist["retry_exhausted_nudge"] == 0
    assert dist["rail_switch_recommended"] == 0


# ---- simulated_recovered_revenue -------------------------------------------


def test_recovered_revenue_uses_at_least_one_success_formula_not_additive(db: Session):
    mandate = make_mandate(db, amount=100_000)  # INR 1000
    event, cls = make_event_and_classification(db, mandate, "P2", ground_truth=True, classified_recoverable=True)
    plan = RetryPlan(
        failure_event_id=event.id,
        classification_id=cls.id,
        decision=PlanDecision.retry,
        reasoning="r",
        expected_value=12345.0,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    # Two steps at p=0.5 each. Additive (wrong) would give 1.0 * amount.
    # Correct "at least one succeeds": 1 - (0.5 * 0.5) = 0.75.
    for i, p in enumerate([0.5, 0.5]):
        db.add(
            PlannedAttempt(
                retry_plan_id=plan.id,
                attempt_number=i + 2,
                proposed_timestamp=OCCURRED_AT,
                implied_notification_timestamp=OCCURRED_AT,
                success_probability=p,
                cost=100.0,
                constraint_reason="ok",
            )
        )
    db.commit()

    revenue = simulated_recovered_revenue(db)
    assert revenue.retry_plan_count == 1
    assert revenue.total_paise == pytest.approx(0.75 * 100_000)
    assert revenue.per_category["P2"] == pytest.approx(0.75 * 100_000)


def test_recovered_revenue_ignores_escalated_plans(db: Session):
    mandate = make_mandate(db)
    event, cls = make_event_and_classification(db, mandate, "P4", ground_truth=False, classified_recoverable=False)
    plan = RetryPlan(
        failure_event_id=event.id,
        classification_id=cls.id,
        decision=PlanDecision.escalate,
        reasoning="r",
    )
    db.add(plan)
    db.commit()

    revenue = simulated_recovered_revenue(db)
    assert revenue.retry_plan_count == 0
    assert revenue.total_paise == 0.0
