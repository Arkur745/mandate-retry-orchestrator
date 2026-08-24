"""Tests for the Day-2 failure simulator."""
import random
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, FailureEvent, Mandate, MandateStatus, Rail
from app.simulator import TAXONOMY, inject_batch, inject_failure


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


def make_mandate(db: Session, rail: Rail, customer_ref: str = "cust_1") -> Mandate:
    mandate = Mandate(
        customer_ref=customer_ref,
        rail=rail,
        amount=99900,
        status=MandateStatus.active,
        mandate_expiry=datetime(2027, 1, 1),
    )
    db.add(mandate)
    db.commit()
    db.refresh(mandate)
    return mandate


def test_random_injection_produces_valid_taxonomy_id_for_rail(db: Session):
    mandate = make_mandate(db, Rail.card_emandate)
    rng = random.Random(1)
    for _ in range(50):
        event = inject_failure(db, mandate, rng=rng)
        assert event.taxonomy_id in TAXONOMY
        assert mandate.rail in TAXONOMY[event.taxonomy_id].rails
        assert event.mandate_id == mandate.id


@pytest.mark.parametrize(
    "taxonomy_id,expected",
    [
        ("P1", True),
        ("P2", True),
        ("P4", False),
        ("P5", False),
        ("P8", False),
        ("P9", True),
    ],
)
def test_ground_truth_recoverable_matches_taxonomy(db: Session, taxonomy_id, expected):
    rail = next(iter(TAXONOMY[taxonomy_id].rails))
    mandate = make_mandate(db, rail)
    event = inject_failure(db, mandate, taxonomy_id=taxonomy_id)
    assert event.ground_truth_recoverable is expected


def test_p12_is_always_ambiguous(db: Session):
    mandate = make_mandate(db, Rail.upi_autopay)
    rng = random.Random(2)
    for _ in range(10):
        event = inject_failure(db, mandate, taxonomy_id="P12", rng=rng)
        assert event.ground_truth_recoverable is None


def test_p7_recoverability_is_consistent_with_chosen_variant(db: Session):
    mandate = make_mandate(db, Rail.upi_autopay)
    rng = random.Random(3)
    seen = set()
    for _ in range(30):
        event = inject_failure(db, mandate, taxonomy_id="P7", rng=rng)
        seen.add(event.ground_truth_recoverable)
        if event.ground_truth_recoverable is True:
            assert "still open" in event.raw_reason_text
        else:
            assert "not acknowledged" in event.raw_reason_text
    # Over 30 draws we should see both branches of the notification window.
    assert seen == {True, False}


def test_p3_recoverability_matches_variant_not_uniform(db: Session):
    # Ground truth revised Day 10 (docs/failure_taxonomy.md revision note,
    # docs/eval_audit.md Day 9 Part B): explicit "Suspected Fraud" variants
    # are non-recoverable; the generic risk-hold variant remains
    # recoverable. No longer a uniform True across all P3 variants.
    mandate = make_mandate(db, Rail.upi_autopay)
    rng = random.Random(4)
    seen = set()
    for _ in range(40):
        event = inject_failure(db, mandate, taxonomy_id="P3", rng=rng)
        seen.add(event.ground_truth_recoverable)
        if "suspected fraud" in event.raw_reason_text.lower():
            assert event.ground_truth_recoverable is False
        else:
            assert event.ground_truth_recoverable is True
    assert seen == {True, False}


def test_forcing_category_incompatible_with_rail_raises(db: Session):
    mandate = make_mandate(db, Rail.card_emandate)
    with pytest.raises(ValueError):
        inject_failure(db, mandate, taxonomy_id="P1")  # P1 is upi_autopay/e_nach only


def test_raw_reason_text_does_not_leak_bare_category_label(db: Session):
    mandate = make_mandate(db, Rail.upi_autopay)
    event = inject_failure(db, mandate, taxonomy_id="P1")
    assert "insufficient balance" not in event.raw_reason_text.lower()


def test_inject_batch_respects_rail_scoping_under_forced_category(db: Session):
    upi = make_mandate(db, Rail.upi_autopay, "cust_upi")
    card = make_mandate(db, Rail.card_emandate, "cust_card")
    rng = random.Random(4)
    # P8 is card_emandate-only; every event must land on the card mandate.
    events = inject_batch(db, [upi, card], 20, taxonomy_id="P8", rng=rng)
    assert len(events) == 20
    assert all(e.mandate_id == card.id for e in events)


def test_inject_batch_with_no_eligible_mandate_raises(db: Session):
    e_nach_only = make_mandate(db, Rail.e_nach)
    with pytest.raises(ValueError):
        # P8 is card_emandate-only; no eligible mandate exists.
        inject_batch(db, [e_nach_only], 5, taxonomy_id="P8")


def test_repeated_injection_does_not_corrupt_existing_events(db: Session):
    mandate = make_mandate(db, Rail.card_emandate)
    first = inject_failure(db, mandate, taxonomy_id="P9")
    second = inject_failure(db, mandate, taxonomy_id="P4")

    assert first.id != second.id
    events = db.query(FailureEvent).filter_by(mandate_id=mandate.id).order_by(FailureEvent.id).all()
    assert [e.id for e in events] == [first.id, second.id]
    assert events[0].taxonomy_id == "P9"
    assert events[1].taxonomy_id == "P4"
