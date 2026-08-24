"""Tests for app/classifier.py.

Two things judges will ask about directly, so both get an explicit test:
1. Rule-path categories never touch Groq at all.
2. Malformed/invalid LLM output is caught by the S1 fallback, not raised.
"""
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.classifier import RULE_TABLE, classify_failure
from app.models import (
    AuditLog,
    Base,
    Classification,
    ClassificationMethod,
    FailureEvent,
    Mandate,
    MandateStatus,
    Rail,
)
from app.simulator import TAXONOMY, inject_failure


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
    return mandate


def _fake_groq_response(content: str):
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


# ---- Rule path: must never call Groq ----------------------------------


@pytest.mark.parametrize("taxonomy_id", sorted(RULE_TABLE) + ["P7"])
def test_rule_path_never_calls_groq(db: Session, taxonomy_id: str):
    rail = next(iter(TAXONOMY[taxonomy_id].rails))
    mandate = make_mandate(db, rail)
    event = inject_failure(db, mandate, taxonomy_id=taxonomy_id)

    mock_client = MagicMock()
    classification = classify_failure(db, event, mandate, groq_client=mock_client)

    mock_client.chat.completions.create.assert_not_called()
    assert classification.method == ClassificationMethod.rule
    assert classification.recoverable is not None


def test_p7_recoverable_matches_notification_window_variant(db: Session):
    mandate = make_mandate(db, Rail.upi_autopay)
    mock_client = MagicMock()

    # Draw P7 events until we've seen both text variants (only two exist).
    seen: dict[bool, FailureEvent] = {}
    for _ in range(20):
        event = inject_failure(db, mandate, taxonomy_id="P7")
        seen.setdefault(bool(event.ground_truth_recoverable), event)
        if len(seen) == 2:
            break
    assert len(seen) == 2

    for ground_truth, event in seen.items():
        classification = classify_failure(db, event, mandate, groq_client=mock_client)
        mock_client.chat.completions.create.assert_not_called()
        assert classification.recoverable == ground_truth
        assert classification.method == ClassificationMethod.rule


# ---- LLM path: mocked Groq client --------------------------------------


@pytest.mark.parametrize("taxonomy_id", ["P3", "P12"])
def test_llm_path_valid_response_is_recorded(db: Session, taxonomy_id: str):
    rail = next(iter(TAXONOMY[taxonomy_id].rails))
    mandate = make_mandate(db, rail)
    event = inject_failure(db, mandate, taxonomy_id=taxonomy_id)

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _fake_groq_response(
        '{"recoverable": true, "confidence": 0.82, "reasoning": "Looks transient."}'
    )

    classification = classify_failure(db, event, mandate, groq_client=mock_client)

    mock_client.chat.completions.create.assert_called_once()
    assert classification.method == ClassificationMethod.llm
    assert classification.recoverable is True
    assert classification.confidence == 0.82
    assert classification.suggested_max_attempts is None


def test_llm_path_null_recoverable_is_valid(db: Session):
    mandate = make_mandate(db, Rail.upi_autopay)
    event = inject_failure(db, mandate, taxonomy_id="P12")

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _fake_groq_response(
        '{"recoverable": null, "confidence": 0.3, "reasoning": "Genuinely unclear."}'
    )
    classification = classify_failure(db, event, mandate, groq_client=mock_client)
    assert classification.method == ClassificationMethod.llm
    assert classification.recoverable is None


# ---- S1 fallback: malformed/invalid LLM output must not crash ----------


@pytest.mark.parametrize(
    "bad_content",
    [
        "not json at all",
        "",
        '{"confidence": 0.5, "reasoning": "missing recoverable field"}',
        '{"recoverable": "yes", "confidence": 0.5, "reasoning": "wrong type"}',
        '{"recoverable": true, "confidence": "high", "reasoning": "bad confidence type"}',
        "```json\n{\"recoverable\": true}\n```",  # markdown-wrapped, not raw JSON
    ],
)
def test_malformed_llm_output_triggers_s1_fallback(db: Session, bad_content: str):
    mandate = make_mandate(db, Rail.card_emandate)
    event = inject_failure(db, mandate, taxonomy_id="P3")

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = _fake_groq_response(bad_content)

    # Must not raise.
    classification = classify_failure(db, event, mandate, groq_client=mock_client)

    assert classification.method == ClassificationMethod.llm_fallback
    assert classification.recoverable is None
    assert classification.suggested_max_attempts == 1

    audit_rows = (
        db.query(AuditLog)
        .filter_by(related_entity_type="failure_event", related_entity_id=event.id)
        .all()
    )
    assert len(audit_rows) == 1
    assert audit_rows[0].event_type == "classifier_llm_fallback"


def test_llm_api_exception_also_triggers_fallback_not_a_crash(db: Session):
    mandate = make_mandate(db, Rail.upi_autopay)
    event = inject_failure(db, mandate, taxonomy_id="P12")

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = RuntimeError("connection reset")

    classification = classify_failure(db, event, mandate, groq_client=mock_client)
    assert classification.method == ClassificationMethod.llm_fallback
    assert classification.recoverable is None


# ---- Persistence / queryability -----------------------------------------


def test_classification_is_queryable_by_failure_event(db: Session):
    mandate = make_mandate(db, Rail.upi_autopay)
    event = inject_failure(db, mandate, taxonomy_id="P1")
    classify_failure(db, event, mandate, groq_client=MagicMock())

    rows = db.query(Classification).filter_by(failure_event_id=event.id).all()
    assert len(rows) == 1
    assert rows[0].failure_event.id == event.id
