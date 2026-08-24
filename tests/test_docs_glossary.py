"""Tests for app/docs_glossary.py (doc-parsing for the trace viewer's
/help page and pinned demo-scenario sidebar) and the two read-only API
endpoints it backs."""
from fastapi.testclient import TestClient

from app.docs_glossary import (
    BADGE_GLOSSARY,
    ESCALATION_TYPE_GLOSSARY,
    parse_demo_scenarios,
    parse_taxonomy_table,
)
from app.main import app
from app.models import EscalationType


def test_parse_taxonomy_table_covers_p1_to_p12():
    rows = parse_taxonomy_table()
    ids = [r["id"] for r in rows]
    assert ids == [f"P{i}" for i in range(1, 13)]
    for row in rows:
        assert row["rail"]
        assert row["failure"]
        assert row["retry_worthy"]


def test_escalation_type_glossary_matches_enum():
    enum_values = {t.value for t in EscalationType}
    assert set(ESCALATION_TYPE_GLOSSARY.keys()) == enum_values
    for desc in ESCALATION_TYPE_GLOSSARY.values():
        assert len(desc) > 10


def test_parse_demo_scenarios_returns_five_curated_mandates():
    rows = parse_demo_scenarios()
    assert len(rows) == 5
    ids = [r["mandate_id"] for r in rows]
    assert ids == sorted(ids)
    for row in rows:
        assert row["scenario"]
        assert row["camera_notes"]


def test_badge_glossary_has_expected_keys():
    assert set(BADGE_GLOSSARY.keys()) == {
        "rule",
        "llm",
        "safe_default",
        "retry",
        "escalate",
        "outcome_success",
        "outcome_failed",
        "outcome_noop",
    }
    for desc in BADGE_GLOSSARY.values():
        assert len(desc) > 10


def test_glossary_endpoint():
    client = TestClient(app)
    resp = client.get("/api/glossary")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["taxonomy"]) == 12
    assert set(body["escalation_types"].keys()) == {t.value for t in EscalationType}
    assert body["badges"] == BADGE_GLOSSARY


def test_demo_scenarios_endpoint():
    client = TestClient(app)
    resp = client.get("/api/demo-scenarios")
    assert resp.status_code == 200
    assert len(resp.json()) == 5


def test_help_page_served():
    client = TestClient(app)
    resp = client.get("/help")
    assert resp.status_code == 200
    assert b"Legend" in resp.content
