"""Read-only mandate endpoints, plus the trace viewer's data API
(GET /mandates/{id}/trace) and escalation filters on GET /mandates.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import EscalationType, FailureEvent, FallbackMessage, Mandate, MandateStatus, PlanDecision, Rail, RetryPlan
from app.trace import TraceEntry, build_mandate_trace

router = APIRouter(prefix="/mandates", tags=["mandates"])


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
