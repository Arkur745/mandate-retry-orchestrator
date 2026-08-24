"""Tests for app/planner.py."""
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    Classification,
    ClassificationMethod,
    FailureEvent,
    Mandate,
    MandateStatus,
    PlanDecision,
    Rail,
)
from app.planner import plan_retries

OCCURRED_AT = datetime(2026, 8, 24, 9, 0, 0)  # 14:30 IST


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


def make_mandate(db: Session, rail: Rail, amount: int = 99900, expiry: datetime = datetime(2027, 1, 1)) -> Mandate:
    mandate = Mandate(
        customer_ref="cust_1",
        rail=rail,
        amount=amount,
        status=MandateStatus.active,
        mandate_expiry=expiry,
    )
    db.add(mandate)
    db.commit()
    db.refresh(mandate)
    return mandate


def make_event(db: Session, mandate: Mandate, taxonomy_id: str, occurred_at: datetime = OCCURRED_AT) -> FailureEvent:
    event = FailureEvent(
        mandate_id=mandate.id,
        taxonomy_id=taxonomy_id,
        raw_reason_text="synthetic",
        occurred_at=occurred_at,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def make_classification(
    db: Session,
    event: FailureEvent,
    *,
    recoverable: bool | None,
    confidence: float | None = 1.0,
    method: ClassificationMethod = ClassificationMethod.rule,
    suggested_max_attempts: int | None = None,
) -> Classification:
    cls = Classification(
        failure_event_id=event.id,
        method=method,
        recoverable=recoverable,
        confidence=confidence,
        reasoning="synthetic",
        suggested_max_attempts=suggested_max_attempts,
    )
    db.add(cls)
    db.commit()
    db.refresh(cls)
    return cls


# ---- P2: front-loaded plan (fast technical, UPI rail avoids the e_nach/card spacing floor) ----


def _run(db, rail, taxonomy_id, **cls_kwargs):
    mandate = make_mandate(db, rail)
    event = make_event(db, mandate, taxonomy_id)
    cls = make_classification(db, event, **cls_kwargs)
    plan = plan_retries(db, event, cls, mandate)
    return mandate, event, cls, plan


class TestP2FrontLoaded:
    def test_plan_is_retry_and_front_loaded(self, db: Session):
        _, event, _, plan = _run(db, Rail.upi_autopay, "P2", recoverable=True, confidence=0.9)
        assert plan.decision == PlanDecision.retry
        assert len(plan.steps) >= 1
        # Every step lands within 24h of the original failure -- "front-loaded".
        for step in plan.steps:
            assert step.proposed_timestamp - event.occurred_at <= timedelta(hours=24)

    def test_probability_decays_across_steps(self, db: Session):
        _, _, _, plan = _run(db, Rail.upi_autopay, "P2", recoverable=True, confidence=0.9)
        probs = [s.success_probability for s in plan.steps]
        assert probs == sorted(probs, reverse=True)
        assert len(set(probs)) == len(probs)  # strictly decreasing, not flat


# ---- P1: spread-out plan (delayed funds) ----


class TestP1SpreadOut:
    def test_plan_is_retry_and_spread_over_days(self, db: Session):
        _, event, _, plan = _run(db, Rail.e_nach, "P1", recoverable=True, confidence=0.9)
        assert plan.decision == PlanDecision.retry
        assert len(plan.steps) >= 1
        # At least one step should be more than 24h out -- "spread-out", not clustered in hours.
        assert any(
            step.proposed_timestamp - event.occurred_at >= timedelta(hours=48)
            for step in plan.steps
        ) or (plan.steps[0].proposed_timestamp - event.occurred_at) >= timedelta(hours=24)

    def test_search_prefers_higher_ev_length_over_first_found(self, db: Session):
        # e_nach's 3-total-attempt cap makes the 3rd retry slot (168h) infeasible,
        # leaving a genuine 1-step vs 2-step choice -- the 2-step plan has higher EV
        # and must be the one selected, proving the search isn't just taking the
        # first valid candidate.
        _, _, _, plan = _run(db, Rail.e_nach, "P1", recoverable=True, confidence=0.9)
        assert plan.decision == PlanDecision.retry
        assert len(plan.steps) == 2
        assert plan.expected_value > 0


# ---- Non-recoverable: escalate, zero constraint-store calls wasted ----


class TestEscalateOnNonRecoverable:
    @pytest.mark.parametrize("taxonomy_id", ["P4", "P5"])
    def test_escalates_with_no_constraint_calls(self, db: Session, taxonomy_id: str):
        mandate = make_mandate(db, Rail.upi_autopay)
        event = make_event(db, mandate, taxonomy_id)
        cls = make_classification(db, event, recoverable=False, confidence=1.0)

        with patch("app.planner.check_retry") as mock_check:
            plan = plan_retries(db, event, cls, mandate)

        mock_check.assert_not_called()
        assert plan.decision == PlanDecision.escalate
        assert plan.expected_value is None
        assert len(plan.steps) == 0
        assert taxonomy_id in plan.reasoning or "recoverable" in plan.reasoning.lower()

    def test_low_confidence_also_escalates_with_no_constraint_calls(self, db: Session):
        mandate = make_mandate(db, Rail.upi_autopay)
        event = make_event(db, mandate, "P12")
        cls = make_classification(
            db, event, recoverable=True, confidence=0.2, method=ClassificationMethod.llm
        )

        with patch("app.planner.check_retry") as mock_check:
            plan = plan_retries(db, event, cls, mandate)

        mock_check.assert_not_called()
        assert plan.decision == PlanDecision.escalate

    def test_none_recoverable_escalates(self, db: Session):
        mandate = make_mandate(db, Rail.upi_autopay)
        event = make_event(db, mandate, "P12")
        cls = make_classification(
            db, event, recoverable=None, confidence=None, method=ClassificationMethod.llm_fallback,
            suggested_max_attempts=1,
        )
        plan = plan_retries(db, event, cls, mandate)
        assert plan.decision == PlanDecision.escalate


# ---- Every candidate sequence vetoed -> escalate, not a crash / empty "success" ----


class TestAllVetoedEscalates:
    def test_mandate_expiring_imminently_escalates_not_crashes(self, db: Session):
        # Expiry before even the fastest candidate offset (0.5h) can land.
        soon_expiry = OCCURRED_AT + timedelta(minutes=5)
        mandate = make_mandate(db, Rail.upi_autopay, expiry=soon_expiry)
        event = make_event(db, mandate, "P2")
        cls = make_classification(db, event, recoverable=True, confidence=0.9)

        plan = plan_retries(db, event, cls, mandate)  # must not raise

        assert plan.decision == PlanDecision.escalate
        assert plan.expected_value is None
        assert len(plan.steps) == 0
        assert "vetoed" in plan.reasoning.lower()

    def test_p9_on_card_emandate_is_structurally_infeasible_and_escalates(self, db: Session):
        # Real finding: card_emandate's generic 24h-spacing floor (Day 4) conflicts
        # with P9's fast sub-day offsets even for the first retry (spacing is
        # measured from the original failure too) -- every candidate is vetoed.
        mandate = make_mandate(db, Rail.card_emandate)
        event = make_event(db, mandate, "P9")
        cls = make_classification(db, event, recoverable=True, confidence=0.9)

        plan = plan_retries(db, event, cls, mandate)

        assert plan.decision == PlanDecision.escalate
        assert len(plan.steps) == 0


# ---- Plan is queryable / persisted correctly ----


def test_plan_and_steps_are_persisted_and_queryable(db: Session):
    mandate, event, cls, plan = _run(db, Rail.upi_autopay, "P2", recoverable=True, confidence=0.9)
    reloaded = db.get(type(plan), plan.id)
    assert reloaded is not None
    assert reloaded.failure_event_id == event.id
    assert reloaded.classification_id == cls.id
    assert len(reloaded.steps) == len(plan.steps)
    for step in reloaded.steps:
        assert step.implied_notification_timestamp == step.proposed_timestamp - timedelta(hours=24)
