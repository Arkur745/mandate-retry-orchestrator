"""Mostly read-only mandate endpoints: the trace viewer's data API
(GET /mandates/{id}/trace), escalation filters on GET /mandates, and the
one write path in this router -- POST /mandates/{id}/simulate-failure,
the interactive fault simulator.

The simulate endpoint is pure orchestration: it calls
app.simulator/classifier/planner/executor/fallback exactly as they exist,
unchanged. No pipeline decision logic lives here.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.classifier import classify_failure
from app.db import get_db
from app.docs_glossary import parse_taxonomy_table
from app.executor import Clock, claim_and_execute, schedule_retry_plan
from app.fallback import generate_fallback_message
from app.models import (
    AuditLog,
    EscalationType,
    FailureEvent,
    FallbackMessage,
    Mandate,
    MandateStatus,
    PlanDecision,
    Rail,
    RetryPlan,
)
from app.planner import plan_retries
from app.simulator import categories_for_rail, inject_failure
from app.trace import TraceEntry, build_mandate_trace

router = APIRouter(prefix="/mandates", tags=["mandates"])

# Reuses the exact Clock abstraction app.executor already uses for its own
# fast-forward tests (see tests/test_executor.py's
# test_fast_forward_and_real_time_clocks_produce_identical_outcomes and
# test_scheduler_fires_and_executes_job, which use comparable scales) --
# not a new mechanism. This endpoint calls claim_and_execute directly for
# each scheduled attempt rather than going through RetryScheduler's
# APScheduler wiring, because a UI-triggered simulate call must return
# the completed trace synchronously within one HTTP request/response --
# RetryScheduler exists to fire jobs asynchronously in a long-running
# service, which is explicitly not what a "run it now" demo button wants.
SIMULATE_CLOCK_SCALE = 1_000_000.0


class MandateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    customer_ref: str
    rail: Rail
    amount: int
    status: MandateStatus
    mandate_expiry: datetime
    created_at: datetime


class TraceEntryOut(BaseModel):
    timestamp: datetime
    entry_type: str
    label: str
    is_llm: bool
    detail: dict


class TaxonomyOptionOut(BaseModel):
    id: str
    description: str


class SimulateFailureIn(BaseModel):
    taxonomy_id: str


@router.get("", response_model=list[MandateOut])
def list_mandates(
    db: Session = Depends(get_db),
    has_escalation: bool | None = Query(
        None, description="Filter to mandates with (True) or without (False) any escalated retry_plan"
    ),
    escalation_type: EscalationType | None = Query(
        None, description="Filter to mandates with at least one fallback message of this escalation_type"
    ),
) -> list[Mandate]:
    query = db.query(Mandate)

    escalate_exists = (
        db.query(RetryPlan.id)
        .join(FailureEvent, FailureEvent.id == RetryPlan.failure_event_id)
        .filter(FailureEvent.mandate_id == Mandate.id, RetryPlan.decision == PlanDecision.escalate)
    )

    if escalation_type is not None:
        type_exists = (
            db.query(FallbackMessage.id)
            .join(RetryPlan, RetryPlan.id == FallbackMessage.retry_plan_id)
            .join(FailureEvent, FailureEvent.id == RetryPlan.failure_event_id)
            .filter(
                FailureEvent.mandate_id == Mandate.id,
                FallbackMessage.escalation_type == escalation_type,
            )
        )
        query = query.filter(type_exists.exists())
    elif has_escalation is True:
        query = query.filter(escalate_exists.exists())
    elif has_escalation is False:
        query = query.filter(~escalate_exists.exists())

    return query.order_by(Mandate.id).all()


@router.get("/{mandate_id}", response_model=MandateOut)
def get_mandate(mandate_id: int, db: Session = Depends(get_db)) -> Mandate:
    mandate = db.get(Mandate, mandate_id)
    if mandate is None:
        raise HTTPException(status_code=404, detail="Mandate not found")
    return mandate


@router.get("/{mandate_id}/trace", response_model=list[TraceEntryOut])
def get_mandate_trace(mandate_id: int, db: Session = Depends(get_db)) -> list[TraceEntry]:
    mandate = db.get(Mandate, mandate_id)
    if mandate is None:
        raise HTTPException(status_code=404, detail="Mandate not found")
    return build_mandate_trace(db, mandate)


@router.get("/{mandate_id}/simulate-failure/options", response_model=list[TaxonomyOptionOut])
def simulate_failure_options(mandate_id: int, db: Session = Depends(get_db)) -> list[dict]:
    """Taxonomy codes valid for this mandate's rail, for the UI's
    "Simulate a failure" dropdown. Rail-scoping reuses
    app.simulator.categories_for_rail (the same function inject_failure
    uses internally) -- not a re-derived list. Descriptions reuse the same
    parsed docs/failure_taxonomy.md rows /help's glossary renders."""
    mandate = db.get(Mandate, mandate_id)
    if mandate is None:
        raise HTTPException(status_code=404, detail="Mandate not found")

    valid_ids = {c.id for c in categories_for_rail(mandate.rail)}
    descriptions = {row["id"]: row["failure"] for row in parse_taxonomy_table()}
    return [
        {"id": cid, "description": descriptions.get(cid, "")}
        for cid in sorted(valid_ids, key=lambda code: int(code[1:]))
    ]


@router.post("/{mandate_id}/simulate-failure", response_model=list[TraceEntryOut])
def simulate_failure(
    mandate_id: int, body: SimulateFailureIn, db: Session = Depends(get_db)
) -> list[TraceEntry]:
    """Runs one failure synchronously through the full, unmodified
    pipeline (inject -> classify -> plan -> execute or fallback) and
    returns the mandate's updated trace in the same shape GET
    /mandates/{id}/trace already returns, so the frontend can render it
    with the exact same timeline component."""
    mandate = db.get(Mandate, mandate_id)
    if mandate is None:
        raise HTTPException(status_code=404, detail="Mandate not found")

    try:
        # Same validation inject_failure always does -- not re-derived
        # here, so "does P9 apply to e_nach" has exactly one answer in
        # the codebase.
        event = inject_failure(db, mandate, taxonomy_id=body.taxonomy_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Marks this failure_event as UI-triggered (vs. seeded/historical) via
    # the same audit_log correlation mechanism build_mandate_trace already
    # reads -- no schema change, no pipeline module touched. The frontend
    # uses this to flag the most recent simulated run as a live result
    # rather than indistinguishable historical/demo data.
    db.add(
        AuditLog(
            related_entity_type="failure_event",
            related_entity_id=event.id,
            event_type="live_simulation_triggered",
            detail={"taxonomy_id": body.taxonomy_id},
        )
    )
    db.commit()

    classification = classify_failure(db, event, mandate)
    plan = plan_retries(db, event, classification, mandate)

    if plan.decision == PlanDecision.retry:
        clock = Clock(scale=SIMULATE_CLOCK_SCALE)
        rows = schedule_retry_plan(db, plan)
        for row in rows:
            claim_and_execute(db, row.id, mandate, clock=clock)
    else:
        generate_fallback_message(db, plan, event, mandate)

    return build_mandate_trace(db, mandate)
