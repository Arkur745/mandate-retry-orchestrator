"""Tests for app/fallback.py."""
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.fallback import (
    TEMPLATES,
    determine_escalation_type,
    generate_fallback_message,
)
from app.models import (
    AuditLog,
    Base,
    Classification,
    ClassificationMethod,
    EscalationType,
    FallbackMethod,
    FailureEvent,
    Mandate,
    MandateStatus,
    PlanDecision,
    Rail,
)
from app.planner import plan_retries

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


def make_escalated_plan(db: Session, rail: Rail, taxonomy_id: str, *, recoverable, confidence=1.0):
    mandate = Mandate(
        customer_ref="cust_1",
        rail=rail,
        amount=99900,
        status=MandateStatus.active,
        mandate_expiry=datetime(2027, 1, 1),
    )
    db.add(mandate)
    db.commit()
    db.refresh(mandate)

    event = FailureEvent(
        mandate_id=mandate.id, taxonomy_id=taxonomy_id, raw_reason_text="x", occurred_at=OCCURRED_AT
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    cls = Classification(
        failure_event_id=event.id,
        method=ClassificationMethod.rule,
        recoverable=recoverable,
        confidence=confidence,
        reasoning="synthetic",
    )
    db.add(cls)
    db.commit()
    db.refresh(cls)

    plan = plan_retries(db, event, cls, mandate)
    assert plan.decision == PlanDecision.escalate
    return mandate, event, plan


def _fake_groq_response(content: str):
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


def _mock_client_returning(content: str) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_groq_response(content)
    return client


# ---- Escalation-type routing: one test per type ---------------------------


@pytest.mark.parametrize("taxonomy_id", ["P4", "P5"])
def test_expired_or_revoked_routes_to_reauth_needed(db: Session, taxonomy_id: str):
    mandate, event, plan = make_escalated_plan(db, Rail.upi_autopay, taxonomy_id, recoverable=False)
    assert determine_escalation_type(event, plan, mandate) == EscalationType.reauth_needed


def test_card_expired_routes_to_reauth_needed(db: Session):
    mandate, event, plan = make_escalated_plan(db, Rail.card_emandate, "P8", recoverable=False)
    assert determine_escalation_type(event, plan, mandate) == EscalationType.reauth_needed


def test_kyc_frozen_routes_to_merchant_escalation(db: Session):
    mandate, event, plan = make_escalated_plan(db, Rail.upi_autopay, "P11", recoverable=False)
    assert determine_escalation_type(event, plan, mandate) == EscalationType.merchant_escalation


def test_debit_limit_routes_to_merchant_escalation(db: Session):
    mandate, event, plan = make_escalated_plan(db, Rail.upi_autopay, "P6", recoverable=False)
    assert determine_escalation_type(event, plan, mandate) == EscalationType.merchant_escalation


def test_low_confidence_routes_to_retry_exhausted_nudge(db: Session):
    mandate, event, plan = make_escalated_plan(
        db, Rail.upi_autopay, "P12", recoverable=True, confidence=0.2
    )
    assert determine_escalation_type(event, plan, mandate) == EscalationType.retry_exhausted_nudge


def test_p9_structural_veto_routes_to_rail_switch_recommended(db: Session):
    mandate, event, plan = make_escalated_plan(db, Rail.card_emandate, "P9", recoverable=True)
    assert plan.escalation_reason_code.value == "all_candidates_vetoed"
    assert determine_escalation_type(event, plan, mandate) == EscalationType.rail_switch_recommended


def test_mandate_expiring_soon_routes_to_retry_exhausted_nudge_not_rail_switch(db: Session):
    # All-vetoed for a non-structural reason (imminent expiry, P1 is
    # delayed_funds not fast_technical) -- must NOT be misrouted to
    # rail_switch_recommended just because it's also "all candidates vetoed".
    from datetime import timedelta

    mandate = Mandate(
        customer_ref="cust_1",
        rail=Rail.e_nach,
        amount=99900,
        status=MandateStatus.active,
        mandate_expiry=OCCURRED_AT + timedelta(minutes=5),
    )
    db.add(mandate)
    db.commit()
    db.refresh(mandate)
    event = FailureEvent(
        mandate_id=mandate.id, taxonomy_id="P1", raw_reason_text="x", occurred_at=OCCURRED_AT
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    cls = Classification(
        failure_event_id=event.id,
        method=ClassificationMethod.rule,
        recoverable=True,
        confidence=1.0,
        reasoning="synthetic",
    )
    db.add(cls)
    db.commit()
    db.refresh(cls)
    plan = plan_retries(db, event, cls, mandate)
    assert plan.decision == PlanDecision.escalate
    assert plan.escalation_reason_code.value == "all_candidates_vetoed"
    assert determine_escalation_type(event, plan, mandate) == EscalationType.retry_exhausted_nudge


# ---- Validation failures fall back to the safe default, not a crash -------


@pytest.mark.parametrize(
    "bad_content",
    [
        "not json",
        '{"reason_explanation": "x"}',  # missing required key
        '{"reason_explanation": "ok", "action_instruction": "ok", "extra": "nope"}',  # extra key
        '{"reason_explanation": "hi", "action_instruction": "short"}',  # too short (below min_length)
    ],
)
def test_malformed_or_malshaped_llm_output_falls_back_to_safe_default(db: Session, bad_content: str):
    mandate, event, plan = make_escalated_plan(db, Rail.upi_autopay, "P4", recoverable=False)
    client = _mock_client_returning(bad_content)

    result = generate_fallback_message(db, plan, event, mandate, groq_client=client)

    assert result.method == FallbackMethod.safe_default
    assert result.validation_passed is False
    assert result.content == TEMPLATES[EscalationType.reauth_needed].safe_default_text
    audit = db.query(AuditLog).filter_by(event_type="fallback_safe_default_used").all()
    assert len(audit) == 1


def test_ungrounded_content_referencing_wrong_category_falls_back_to_safe_default(db: Session):
    # Actual failure is P4 (expired), but the LLM hallucinates a P3 (fraud)
    # explanation -- must be caught and rejected, not shipped.
    mandate, event, plan = make_escalated_plan(db, Rail.upi_autopay, "P4", recoverable=False)
    ungrounded = (
        '{"reason_explanation": "This was flagged for suspicious activity and fraud '
        'by your bank.", "action_instruction": "Please contact your bank to confirm."}'
    )
    client = _mock_client_returning(ungrounded)

    result = generate_fallback_message(db, plan, event, mandate, groq_client=client)

    assert result.method == FallbackMethod.safe_default
    assert result.validation_passed is False
    assert "fraud" in result.validation_detail.lower() or "risk hold" in result.validation_detail.lower()
    assert "fraud" not in result.content.lower()  # safe default never contains hallucinated content


def test_valid_llm_output_is_used_and_marked_passed(db: Session):
    mandate, event, plan = make_escalated_plan(db, Rail.upi_autopay, "P5", recoverable=False)
    good = (
        '{"reason_explanation": "The mandate was cancelled by you.", '
        '"action_instruction": "Set up a new mandate to continue."}'
    )
    client = _mock_client_returning(good)

    result = generate_fallback_message(db, plan, event, mandate, groq_client=client)

    assert result.method == FallbackMethod.llm
    assert result.validation_passed is True
    assert "cancelled" in result.content.lower()
    assert "Rs " in result.content  # amount injected by code, not the LLM


def test_generate_fallback_message_rejects_non_escalate_plans(db: Session):
    mandate = Mandate(
        customer_ref="c",
        rail=Rail.upi_autopay,
        amount=99900,
        status=MandateStatus.active,
        mandate_expiry=datetime(2027, 1, 1),
    )
    db.add(mandate)
    db.commit()
    db.refresh(mandate)
    event = FailureEvent(mandate_id=mandate.id, taxonomy_id="P2", raw_reason_text="x", occurred_at=OCCURRED_AT)
    db.add(event)
    db.commit()
    db.refresh(event)
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
    retry_plan = plan_retries(db, event, cls, mandate)
    assert retry_plan.decision == PlanDecision.retry

    with pytest.raises(ValueError):
        generate_fallback_message(db, retry_plan, event, mandate)


# ---- P9 end to end: planner escalation -> fallback rail-switch message ----


def test_p9_end_to_end_planner_escalation_to_rail_switch_message(db: Session):
    mandate, event, plan = make_escalated_plan(db, Rail.card_emandate, "P9", recoverable=True)
    assert plan.decision == PlanDecision.escalate

    good = (
        '{"structural_reasoning": "This category needs a fast retry within hours, but this '
        'payment rail requires at least a day between attempts, so no valid retry timing exists."}'
    )
    client = _mock_client_returning(good)

    result = generate_fallback_message(db, plan, event, mandate, groq_client=client)

    assert result.escalation_type == EscalationType.rail_switch_recommended
    assert result.method == FallbackMethod.llm
    assert result.validation_passed is True
    assert "upi_autopay" in result.content
    assert str(mandate.id) in result.content
    assert "card_emandate" in result.content
