"""Tests for app/trace.py (pure trace-assembly logic) and the trace
viewer's API surface (app/routers/mandates.py's trace endpoint and
GET /mandates filters)."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import app
from app.models import (
    AuditLog,
    Base,
    Classification,
    ClassificationMethod,
    EscalationReasonCode,
    EscalationType,
    FailureEvent,
    FallbackMessage,
    FallbackMethod,
    Mandate,
    MandateStatus,
    PlanDecision,
    PlannedAttempt,
    Rail,
    RetryAttempt,
    RetryOutcome,
    RetryPlan,
)
from app.trace import build_mandate_trace

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


def make_mandate(db: Session, rail: Rail = Rail.upi_autopay) -> Mandate:
    m = Mandate(
        customer_ref="cust_1",
        rail=rail,
        amount=99900,
        status=MandateStatus.active,
        mandate_expiry=datetime(2027, 1, 1),
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def make_event(db: Session, mandate: Mandate, taxonomy_id: str = "P2") -> FailureEvent:
    e = FailureEvent(
        mandate_id=mandate.id,
        taxonomy_id=taxonomy_id,
        raw_reason_text="synthetic reason",
        ground_truth_recoverable=True,
        occurred_at=OCCURRED_AT,
    )
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


# ---- build_mandate_trace: full history ------------------------------------


def test_full_trace_ordering_and_completeness(db: Session):
    # Explicit, consistent timestamps throughout -- mixing a fixed
    # historical time with server_default=func.now() (real current time)
    # on other rows would make this test's own data internally
    # inconsistent, not a real trace.py ordering bug.
    mandate = make_mandate(db)
    event = make_event(db, mandate)

    cls = Classification(
        failure_event_id=event.id,
        method=ClassificationMethod.rule,
        recoverable=True,
        confidence=1.0,
        reasoning="Rule lookup: retry-worthy.",
        created_at=OCCURRED_AT + timedelta(seconds=1),
    )
    db.add(cls)
    db.commit()
    db.refresh(cls)

    plan = RetryPlan(
        failure_event_id=event.id,
        classification_id=cls.id,
        decision=PlanDecision.retry,
        reasoning="Selected a 1-step sequence.",
        expected_value=1234.5,
        created_at=OCCURRED_AT + timedelta(seconds=2),
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    db.add(
        PlannedAttempt(
            retry_plan_id=plan.id,
            attempt_number=2,
            proposed_timestamp=OCCURRED_AT + timedelta(hours=1),
            implied_notification_timestamp=OCCURRED_AT - timedelta(hours=23),
            success_probability=0.6,
            cost=200.0,
            constraint_reason="ok",
        )
    )
    db.commit()

    attempt = RetryAttempt(
        failure_event_id=event.id,
        attempt_number=2,
        scheduled_at=OCCURRED_AT + timedelta(hours=1),
        executed_at=OCCURRED_AT + timedelta(hours=1, minutes=1),
        outcome=RetryOutcome.success,
        idempotency_key="k1",
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)

    db.add(
        AuditLog(
            related_entity_type="retry_attempt",
            related_entity_id=attempt.id,
            event_type="retry_attempt_executed",
            detail={"outcome": "success"},
            created_at=OCCURRED_AT + timedelta(hours=1, minutes=2),
        )
    )
    db.commit()

    trace = build_mandate_trace(db, mandate)
    types = [e.entry_type for e in trace]
    assert types == ["failure_event", "classification", "plan", "retry_attempt", "audit_log"]

    # Timestamps must be non-decreasing across the trace (chronological).
    timestamps = [e.timestamp for e in trace]
    assert timestamps == sorted(timestamps)

    assert trace[0].detail["taxonomy_id"] == "P2"
    assert trace[1].is_llm is False
    assert trace[2].detail["steps"][0]["attempt_number"] == 2
    assert trace[3].detail["outcome"] == "success"
    assert trace[4].detail["event_type"] == "retry_attempt_executed"


def test_escalate_with_llm_classification_and_fallback(db: Session):
    mandate = make_mandate(db, rail=Rail.card_emandate)
    event = make_event(db, mandate, taxonomy_id="P3")

    cls = Classification(
        failure_event_id=event.id,
        method=ClassificationMethod.llm,
        recoverable=False,
        confidence=0.9,
        reasoning="Suspected fraud requires bank-side resolution.",
    )
    db.add(cls)
    db.commit()
    db.refresh(cls)

    plan = RetryPlan(
        failure_event_id=event.id,
        classification_id=cls.id,
        decision=PlanDecision.escalate,
        reasoning="Not recoverable.",
        escalation_reason_code=EscalationReasonCode.not_recoverable,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)

    db.add(
        FallbackMessage(
            retry_plan_id=plan.id,
            escalation_type=EscalationType.merchant_escalation,
            template_key="merchant_escalation",
            method=FallbackMethod.llm,
            content="Manual review needed.",
            validation_passed=True,
        )
    )
    db.commit()

    trace = build_mandate_trace(db, mandate)
    types = [e.entry_type for e in trace]
    assert types == ["failure_event", "classification", "plan", "fallback_message"]

    classification_entry = trace[1]
    assert classification_entry.is_llm is True
    assert "fraud" in classification_entry.detail["reasoning"].lower()

    fallback_entry = trace[3]
    assert fallback_entry.is_llm is True
    assert fallback_entry.detail["content"] == "Manual review needed."


# ---- build_mandate_trace: partial / empty ---------------------------------


def test_partial_trace_classification_only_no_plan_yet(db: Session):
    mandate = make_mandate(db)
    event = make_event(db, mandate)
    db.add(
        Classification(
            failure_event_id=event.id,
            method=ClassificationMethod.rule,
            recoverable=False,
            confidence=1.0,
            reasoning="Not retry-worthy.",
        )
    )
    db.commit()

    trace = build_mandate_trace(db, mandate)  # must not raise
    types = [e.entry_type for e in trace]
    assert types == ["failure_event", "classification"]


def test_empty_trace_for_mandate_with_no_failure_events(db: Session):
    mandate = make_mandate(db)
    trace = build_mandate_trace(db, mandate)
    assert trace == []


# ---- API endpoints ----------------------------------------------------------


@pytest.fixture()
def client(db: Session):
    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_trace_endpoint_returns_ordered_json(client: TestClient, db: Session):
    mandate = make_mandate(db)
    event = make_event(db, mandate)
    db.add(
        Classification(
            failure_event_id=event.id,
            method=ClassificationMethod.rule,
            recoverable=True,
            confidence=1.0,
            reasoning="r",
        )
    )
    db.commit()

    resp = client.get(f"/mandates/{mandate.id}/trace")
    assert resp.status_code == 200
    body = resp.json()
    assert [e["entry_type"] for e in body] == ["failure_event", "classification"]
    assert body[0]["label"] == "Failure: P2"


def test_trace_endpoint_404_for_unknown_mandate(client: TestClient):
    resp = client.get("/mandates/99999/trace")
    assert resp.status_code == 404


def test_list_mandates_has_escalation_filter(client: TestClient, db: Session):
    escalated_mandate = make_mandate(db)
    escalated_event = make_event(db, escalated_mandate, "P4")
    cls1 = Classification(
        failure_event_id=escalated_event.id,
        method=ClassificationMethod.rule,
        recoverable=False,
        confidence=1.0,
        reasoning="r",
    )
    db.add(cls1)
    db.commit()
    db.refresh(cls1)
    db.add(
        RetryPlan(
            failure_event_id=escalated_event.id,
            classification_id=cls1.id,
            decision=PlanDecision.escalate,
            reasoning="r",
            escalation_reason_code=EscalationReasonCode.not_recoverable,
        )
    )

    clean_mandate = make_mandate(db)
    clean_event = make_event(db, clean_mandate, "P2")
    cls2 = Classification(
        failure_event_id=clean_event.id,
        method=ClassificationMethod.rule,
        recoverable=True,
        confidence=1.0,
        reasoning="r",
    )
    db.add(cls2)
    db.commit()
    db.refresh(cls2)
    db.add(
        RetryPlan(
            failure_event_id=clean_event.id,
            classification_id=cls2.id,
            decision=PlanDecision.retry,
            reasoning="r",
            expected_value=100.0,
        )
    )
    db.commit()

    resp_true = client.get("/mandates?has_escalation=true")
    ids_true = [m["id"] for m in resp_true.json()]
    assert ids_true == [escalated_mandate.id]

    resp_false = client.get("/mandates?has_escalation=false")
    ids_false = [m["id"] for m in resp_false.json()]
    assert ids_false == [clean_mandate.id]

    resp_all = client.get("/mandates")
    assert len(resp_all.json()) == 2


def test_list_mandates_escalation_type_filter(client: TestClient, db: Session):
    mandate = make_mandate(db, rail=Rail.card_emandate)
    event = make_event(db, mandate, "P9")
    cls = Classification(
        failure_event_id=event.id,
        method=ClassificationMethod.rule,
        recoverable=True,
        confidence=1.0,
        reasoning="r",
    )
    db.add(cls)
    db.commit()
    db.refresh(cls)
    plan = RetryPlan(
        failure_event_id=event.id,
        classification_id=cls.id,
        decision=PlanDecision.escalate,
        reasoning="r",
        escalation_reason_code=EscalationReasonCode.all_candidates_vetoed,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    db.add(
        FallbackMessage(
            retry_plan_id=plan.id,
            escalation_type=EscalationType.rail_switch_recommended,
            template_key="rail_switch_recommended",
            method=FallbackMethod.llm,
            content="Switch to UPI Autopay.",
            validation_passed=True,
        )
    )
    db.commit()

    resp = client.get("/mandates?escalation_type=rail_switch_recommended")
    ids = [m["id"] for m in resp.json()]
    assert ids == [mandate.id]

    resp_other = client.get("/mandates?escalation_type=reauth_needed")
    assert resp_other.json() == []
