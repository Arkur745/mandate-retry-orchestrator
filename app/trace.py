"""Builds a single chronologically-ordered trace for one mandate, pulled
entirely from data that already exists (failure_events, classifications,
retry_plans/planned_attempts, retry_attempts, fallback_messages,
audit_log) -- read-only, no pipeline logic lives here or is touched by it.

Used by app.routers.mandates' GET /mandates/{id}/trace and by
static/trace_viewer.html via that endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Classification,
    FailureEvent,
    FallbackMessage,
    Mandate,
    PlanDecision,
    RetryAttempt,
    RetryPlan,
)

# Human-readable labels for audit_log event types -- falls back to a
# title-cased version of the raw event_type for anything not listed here.
_AUDIT_EVENT_LABELS = {
    "duplicate_schedule_no_op": "Duplicate scheduling blocked (S3)",
    "concurrent_execution_no_op": "Concurrent execution blocked (S4)",
    "api_retry": "API retry (backoff)",
    "retry_attempt_executed": "Execution completed",
    "classifier_llm_fallback": "Classifier LLM fallback triggered (S1)",
    "fallback_safe_default_used": "Fallback safe default used (S8)",
    "stuck_execution_reclaimed": "Stuck execution reclaimed (S9)",
}

# Stage order is the tiebreak when two entries land on the same
# (SQLite CURRENT_TIMESTAMP has only 1-second resolution, so entries
# created in the same batch script within the same second are common).
# Python's sort is stable, so as long as entries are *constructed* in the
# logically correct order to begin with, ties preserve that order.
_STAGE_ORDER = {
    "failure_event": 0,
    "classification": 1,
    "plan": 2,
    "retry_attempt": 3,
    "audit_log": 3,
    "fallback_message": 4,
}


@dataclass
class TraceEntry:
    timestamp: datetime
    entry_type: str
    label: str
    is_llm: bool
    detail: dict = field(default_factory=dict)


def _audit_label(event_type: str) -> str:
    return _AUDIT_EVENT_LABELS.get(event_type, event_type.replace("_", " ").title())


def _classification_entry(cls: Classification) -> TraceEntry:
    is_llm = cls.method.value != "rule"
    if cls.method.value == "rule":
        verdict = "recoverable" if cls.recoverable else "not recoverable"
        label = f"Classified (rule): {verdict}"
    elif cls.method.value == "llm_fallback":
        label = "Classified (LLM fallback -- validation failed): treated as ambiguous"
    else:
        verdict = (
            "recoverable" if cls.recoverable is True
            else "not recoverable" if cls.recoverable is False
            else "ambiguous"
        )
        label = f"Classified (LLM): {verdict}"
    return TraceEntry(
        timestamp=cls.created_at,
        entry_type="classification",
        label=label,
        is_llm=is_llm,
        detail={
            "method": cls.method.value,
            "recoverable": cls.recoverable,
            "confidence": cls.confidence,
            "reasoning": cls.reasoning,
            "suggested_max_attempts": cls.suggested_max_attempts,
        },
    )


def _plan_entry(plan: RetryPlan) -> TraceEntry:
    if plan.decision == PlanDecision.retry:
        label = f"Plan: retry ({len(plan.steps)} attempt{'s' if len(plan.steps) != 1 else ''})"
        steps_detail = [
            {
                "attempt_number": s.attempt_number,
                "proposed_timestamp": s.proposed_timestamp.isoformat(),
                "success_probability": s.success_probability,
                "cost": s.cost,
                "constraint_reason": s.constraint_reason,
            }
            for s in plan.steps
        ]
    else:
        reason_code = plan.escalation_reason_code.value if plan.escalation_reason_code else "unknown"
        label = f"Plan: escalate ({reason_code})"
        steps_detail = []
    return TraceEntry(
        timestamp=plan.created_at,
        entry_type="plan",
        label=label,
        is_llm=False,  # app.planner has zero LLM involvement, by design
        detail={
            "decision": plan.decision.value,
            "reasoning": plan.reasoning,
            "expected_value": plan.expected_value,
            "escalation_reason_code": plan.escalation_reason_code.value if plan.escalation_reason_code else None,
            "steps": steps_detail,
        },
    )


def _retry_attempt_entry(attempt: RetryAttempt) -> TraceEntry:
    timestamp = attempt.executed_at or attempt.scheduled_at
    return TraceEntry(
        timestamp=timestamp,
        entry_type="retry_attempt",
        label=f"Attempt #{attempt.attempt_number}: {attempt.outcome.value}",
        is_llm=False,
        detail={
            "attempt_number": attempt.attempt_number,
            "outcome": attempt.outcome.value,
            "scheduled_at": attempt.scheduled_at.isoformat(),
            "executed_at": attempt.executed_at.isoformat() if attempt.executed_at else None,
        },
    )


def _fallback_message_entry(msg: FallbackMessage) -> TraceEntry:
    return TraceEntry(
        timestamp=msg.created_at,
        entry_type="fallback_message",
        label=f"Fallback message: {msg.escalation_type.value} ({msg.method.value})",
        is_llm=msg.method.value == "llm",
        detail={
            "escalation_type": msg.escalation_type.value,
            "template_key": msg.template_key,
            "method": msg.method.value,
            "content": msg.content,
            "validation_passed": msg.validation_passed,
            "validation_detail": msg.validation_detail,
        },
    )


def _audit_log_entry(row: AuditLog) -> TraceEntry:
    return TraceEntry(
        timestamp=row.created_at,
        entry_type="audit_log",
        label=_audit_label(row.event_type),
        is_llm=False,
        detail={
            "event_type": row.event_type,
            "related_entity_type": row.related_entity_type,
            "related_entity_id": row.related_entity_id,
            "detail": row.detail,
        },
    )


def build_mandate_trace(db: Session, mandate: Mandate) -> list[TraceEntry]:
    """Chronologically-ordered trace covering every failure_event this
    mandate has had, and everything downstream of each (classification,
    plan, retry_attempts, fallback message, related audit_log rows)."""
    entries: list[TraceEntry] = []

    events = (
        db.query(FailureEvent)
        .filter_by(mandate_id=mandate.id)
        .order_by(FailureEvent.occurred_at)
        .all()
    )

    for event in events:
        entries.append(
            TraceEntry(
                timestamp=event.occurred_at,
                entry_type="failure_event",
                label=f"Failure: {event.taxonomy_id}",
                is_llm=False,
                detail={
                    "taxonomy_id": event.taxonomy_id,
                    "raw_reason_text": event.raw_reason_text,
                    "ground_truth_recoverable": event.ground_truth_recoverable,
                },
            )
        )

        classifications = (
            db.query(Classification)
            .filter_by(failure_event_id=event.id)
            .order_by(Classification.created_at)
            .all()
        )
        for cls in classifications:
            entries.append(_classification_entry(cls))

        plans = (
            db.query(RetryPlan)
            .filter_by(failure_event_id=event.id)
            .order_by(RetryPlan.created_at)
            .all()
        )

        attempt_ids: list[int] = []
        plan_ids: list[int] = []
        for plan in plans:
            plan_ids.append(plan.id)
            entries.append(_plan_entry(plan))

            for msg in plan.fallback_messages:
                entries.append(_fallback_message_entry(msg))

        retry_attempts = (
            db.query(RetryAttempt)
            .filter_by(failure_event_id=event.id)
            .order_by(RetryAttempt.attempt_number)
            .all()
        )
        for attempt in retry_attempts:
            attempt_ids.append(attempt.id)
            entries.append(_retry_attempt_entry(attempt))

        audit_rows = (
            db.query(AuditLog)
            .filter(
                (
                    (AuditLog.related_entity_type == "failure_event")
                    & (AuditLog.related_entity_id == event.id)
                )
                | (
                    (AuditLog.related_entity_type == "retry_plan")
                    & (AuditLog.related_entity_id.in_(plan_ids or [-1]))
                )
                | (
                    (AuditLog.related_entity_type == "retry_attempt")
                    & (AuditLog.related_entity_id.in_(attempt_ids or [-1]))
                )
            )
            .order_by(AuditLog.created_at)
            .all()
        )
        for row in audit_rows:
            entries.append(_audit_log_entry(row))

    # Sort key truncates to whole-second precision, not the full sort key:
    # SQLite's CURRENT_TIMESTAMP (used for created_at server defaults) has
    # only 1-second resolution, while some Python-side timestamps
    # (RetryAttempt.executed_at, stamped via app.executor's Clock) carry
    # microseconds. Comparing those directly can make an audit_log row
    # that was actually written a moment *after* the attempt it describes
    # sort *before* it, just because its truncated value happens to be
    # numerically smaller. Truncating the sort key (not the displayed
    # `timestamp` field, which keeps full precision) means same-second
    # entries fall back to stage order + construction order (stable sort),
    # which reflects the real causal sequence.
    entries.sort(
        key=lambda e: (e.timestamp.replace(microsecond=0), _STAGE_ORDER.get(e.entry_type, 99))
    )
    return entries
