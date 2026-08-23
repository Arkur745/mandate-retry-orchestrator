"""Model-layer tests: can a mandate + failure_event + retry_attempt be linked?"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    FailureEvent,
    Mandate,
    MandateStatus,
    Rail,
    RetryAttempt,
    RetryOutcome,
)


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


def make_mandate(**overrides) -> Mandate:
    defaults = dict(
        customer_ref="cust_1",
        rail=Rail.upi_autopay,
        amount=50000,
        status=MandateStatus.active,
        mandate_expiry=datetime(2027, 1, 1),
    )
    defaults.update(overrides)
    return Mandate(**defaults)


def test_create_and_link_mandate_failure_event_retry_attempt(db: Session):
    mandate = make_mandate()
    db.add(mandate)
    db.commit()

    failure = FailureEvent(
        mandate_id=mandate.id,
        taxonomy_id="P1",
        raw_reason_text="Insufficient balance at debit time",
        ground_truth_recoverable=True,
        occurred_at=datetime(2026, 8, 23, 9, 0, 0),
    )
    db.add(failure)
    db.commit()

    attempt = RetryAttempt(
        failure_event_id=failure.id,
        attempt_number=1,
        scheduled_at=datetime(2026, 8, 24, 9, 0, 0),
        outcome=RetryOutcome.pending,
        idempotency_key=f"mandate:{mandate.id}:failure:{failure.id}:attempt:1",
    )
    db.add(attempt)
    db.commit()

    # Traverse the relationships end to end.
    reloaded = db.get(Mandate, mandate.id)
    assert reloaded is not None
    assert len(reloaded.failure_events) == 1
    assert reloaded.failure_events[0].taxonomy_id == "P1"
    assert len(reloaded.failure_events[0].retry_attempts) == 1
    assert reloaded.failure_events[0].retry_attempts[0].outcome == RetryOutcome.pending
    assert reloaded.failure_events[0].retry_attempts[0].failure_event is reloaded.failure_events[0]


def test_idempotency_key_is_unique(db: Session):
    mandate = make_mandate()
    db.add(mandate)
    db.commit()

    failure = FailureEvent(
        mandate_id=mandate.id,
        taxonomy_id="S3",
        raw_reason_text="Duplicate webhook delivery",
        occurred_at=datetime(2026, 8, 23, 9, 0, 0),
    )
    db.add(failure)
    db.commit()

    key = f"mandate:{mandate.id}:failure:{failure.id}:attempt:1"
    db.add(
        RetryAttempt(
            failure_event_id=failure.id,
            attempt_number=1,
            scheduled_at=datetime(2026, 8, 24, 9, 0, 0),
            idempotency_key=key,
        )
    )
    db.commit()

    db.add(
        RetryAttempt(
            failure_event_id=failure.id,
            attempt_number=1,
            scheduled_at=datetime(2026, 8, 24, 9, 0, 0),
            idempotency_key=key,
        )
    )
    with pytest.raises(Exception):
        db.commit()


def test_mandate_cascade_delete_removes_failure_events(db: Session):
    mandate = make_mandate()
    db.add(mandate)
    db.commit()

    db.add(
        FailureEvent(
            mandate_id=mandate.id,
            taxonomy_id="P4",
            raw_reason_text="Mandate expired",
            ground_truth_recoverable=False,
            occurred_at=datetime(2026, 8, 23, 9, 0, 0),
        )
    )
    db.commit()
    assert db.query(FailureEvent).count() == 1

    db.delete(mandate)
    db.commit()
    assert db.query(FailureEvent).count() == 0
