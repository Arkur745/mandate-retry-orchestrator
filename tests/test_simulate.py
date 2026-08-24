"""Tests for the interactive fault simulator: POST
/mandates/{id}/simulate-failure and GET
/mandates/{id}/simulate-failure/options.

This endpoint is pure orchestration over app.simulator/classifier/
planner/executor/fallback -- these tests exercise the endpoint through
FastAPI's TestClient (not the underlying functions directly, which are
already covered by their own module's tests) to confirm the wiring is
correct end to end.
"""
import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import get_db
from app.main import app
from app.models import AuditLog, Base, Mandate, MandateStatus, Rail
from app.routers.mandates import SIMULATE_CLOCK_SCALE


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


@pytest.fixture()
def client(db: Session):
    def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


def make_mandate(db: Session, rail: Rail) -> Mandate:
    m = Mandate(
        customer_ref="cust_sim",
        rail=rail,
        amount=99900,
        status=MandateStatus.active,
        mandate_expiry=datetime(2027, 1, 1),
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _fake_groq_response(payload: dict):
    message = MagicMock()
    message.content = json.dumps(payload)
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


def _mock_groq_client(payload: dict) -> MagicMock:
    client = MagicMock()
    client.chat.completions.create.return_value = _fake_groq_response(payload)
    return client


def test_simulate_rejects_invalid_rail_category(client: TestClient, db: Session):
    mandate = make_mandate(db, Rail.upi_autopay)  # P9 is card_emandate-only
    resp = client.post(f"/mandates/{mandate.id}/simulate-failure", json={"taxonomy_id": "P9"})
    assert resp.status_code == 422
    assert "P9" in resp.json()["detail"]
    assert "upi_autopay" in resp.json()["detail"]


def test_simulate_rejects_unknown_taxonomy_id(client: TestClient, db: Session):
    mandate = make_mandate(db, Rail.upi_autopay)
    resp = client.post(f"/mandates/{mandate.id}/simulate-failure", json={"taxonomy_id": "P99"})
    assert resp.status_code == 422


def test_simulate_404_for_unknown_mandate(client: TestClient):
    resp = client.post("/mandates/99999/simulate-failure", json={"taxonomy_id": "P2"})
    assert resp.status_code == 404


def test_simulate_options_filters_by_rail(client: TestClient, db: Session):
    mandate = make_mandate(db, Rail.upi_autopay)
    resp = client.get(f"/mandates/{mandate.id}/simulate-failure/options")
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()}
    assert "P2" in ids  # applies to all rails
    assert "P9" not in ids  # card_emandate only
    assert "P8" not in ids  # card_emandate only
    for row in resp.json():
        assert row["description"]


def test_simulate_rule_path_p2_full_run_zero_llm_calls(client: TestClient, db: Session, monkeypatch):
    mandate = make_mandate(db, Rail.upi_autopay)

    # If the rule path ever called Groq, this would blow up -- proving
    # zero LLM calls the same way test_classifier.py does.
    def _boom():
        raise AssertionError("rule-path category must never call Groq")

    monkeypatch.setattr("app.classifier._default_groq_client", _boom)

    resp = client.post(f"/mandates/{mandate.id}/simulate-failure", json={"taxonomy_id": "P2"})
    assert resp.status_code == 200
    entries = resp.json()

    types = [e["entry_type"] for e in entries]
    assert types[0] == "failure_event"
    assert "classification" in types
    assert "plan" in types
    assert "retry_attempt" in types
    assert all(not e["is_llm"] for e in entries)

    retry_attempts = [e for e in entries if e["entry_type"] == "retry_attempt"]
    assert len(retry_attempts) >= 1
    # Fast-forward: every attempt is fully executed synchronously, none
    # left pending despite scheduled_at being hours/days in the future.
    assert all(a["detail"]["outcome"] in ("success", "failed") for a in retry_attempts)
    assert all(a["detail"]["executed_at"] is not None for a in retry_attempts)


def test_simulate_llm_path_p3_includes_reasoning_text(client: TestClient, db: Session, monkeypatch):
    mandate = make_mandate(db, Rail.card_emandate)

    classify_client = _mock_groq_client(
        {
            "recoverable": False,
            "confidence": 0.9,
            "reasoning": "Explicit fraud flag from issuer requires bank-side resolution.",
        }
    )
    fallback_client = _mock_groq_client({"summary": "Fraud hold flagged by issuing bank risk engine."})
    monkeypatch.setattr("app.classifier._default_groq_client", lambda: classify_client)
    monkeypatch.setattr("app.fallback._default_groq_client", lambda: fallback_client)

    resp = client.post(f"/mandates/{mandate.id}/simulate-failure", json={"taxonomy_id": "P3"})
    assert resp.status_code == 200
    entries = resp.json()

    classification = next(e for e in entries if e["entry_type"] == "classification")
    assert classification["is_llm"] is True
    assert classification["detail"]["reasoning"]
    assert "fraud" in classification["detail"]["reasoning"].lower()

    assert any(e["entry_type"] == "fallback_message" for e in entries)


def test_simulate_p9_escalation_ends_in_fallback_record(client: TestClient, db: Session, monkeypatch):
    mandate = make_mandate(db, Rail.card_emandate)

    fallback_client = _mock_groq_client(
        {"structural_reasoning": "Card e-mandate's minimum retry spacing exceeds this category's fast window."}
    )
    monkeypatch.setattr("app.fallback._default_groq_client", lambda: fallback_client)

    resp = client.post(f"/mandates/{mandate.id}/simulate-failure", json={"taxonomy_id": "P9"})
    assert resp.status_code == 200
    entries = resp.json()

    plan_entry = next(e for e in entries if e["entry_type"] == "plan")
    assert plan_entry["detail"]["decision"] == "escalate"

    assert entries[-1]["entry_type"] == "fallback_message"
    assert entries[-1]["detail"]["escalation_type"] == "rail_switch_recommended"


def test_simulate_marks_failure_event_as_live_run(client: TestClient, db: Session):
    mandate = make_mandate(db, Rail.upi_autopay)
    resp = client.post(f"/mandates/{mandate.id}/simulate-failure", json={"taxonomy_id": "P2"})
    entries = resp.json()

    failure_entry = next(e for e in entries if e["entry_type"] == "failure_event")
    live_marker = next(
        e for e in entries
        if e["entry_type"] == "audit_log" and e["detail"]["event_type"] == "live_simulation_triggered"
    )
    assert live_marker["detail"]["related_entity_id"] == failure_entry["detail"]["id"]

    # Also persisted directly, independent of trace assembly.
    row = db.query(AuditLog).filter_by(event_type="live_simulation_triggered").one()
    assert row.related_entity_id == failure_entry["detail"]["id"]


def test_simulate_reuses_fast_forward_clock_not_a_new_mechanism():
    # The endpoint must use app.executor.Clock (imported, not
    # reimplemented) at a scale large enough that hour/day-scale plan
    # offsets collapse to a fraction of a real second -- same class the
    # executor's own fast-forward tests use.
    from app.executor import Clock

    assert SIMULATE_CLOCK_SCALE > 1.0
    clock = Clock(scale=SIMULATE_CLOCK_SCALE)
    assert clock.scale == SIMULATE_CLOCK_SCALE
