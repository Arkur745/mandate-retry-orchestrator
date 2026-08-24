"""SQLAlchemy models: mandates, failure_events, retry_attempts, audit_log.

Day 1 scope only — no classifier/planner/executor logic lives here, just the
schema those stages will read and write.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Rail(str, enum.Enum):
    upi_autopay = "upi_autopay"
    e_nach = "e_nach"
    card_emandate = "card_emandate"


class MandateStatus(str, enum.Enum):
    created = "created"
    active = "active"
    paused = "paused"
    revoked = "revoked"
    expired = "expired"


class RetryOutcome(str, enum.Enum):
    pending = "pending"
    executing = "executing"
    success = "success"
    failed = "failed"
    skipped = "skipped"


class ClassificationMethod(str, enum.Enum):
    rule = "rule"
    llm = "llm"
    llm_fallback = "llm_fallback"


class PlanDecision(str, enum.Enum):
    retry = "retry"
    escalate = "escalate"


class EscalationReasonCode(str, enum.Enum):
    """Structured reason a RetryPlan escalated -- set alongside the
    free-text reasoning so downstream consumers (app.fallback) don't have
    to parse prose. None for 'retry' decisions."""

    not_recoverable = "not_recoverable"
    low_confidence = "low_confidence"
    no_timing_profile = "no_timing_profile"
    all_candidates_vetoed = "all_candidates_vetoed"
    negative_expected_value = "negative_expected_value"


class EscalationType(str, enum.Enum):
    """Which fallback-message template family applies. Computed
    deterministically in app.fallback from (taxonomy_id,
    escalation_reason_code, mandate.rail) -- not an LLM decision, see
    app/fallback.py's module docstring for why."""

    retry_exhausted_nudge = "retry_exhausted_nudge"
    reauth_needed = "reauth_needed"
    rail_switch_recommended = "rail_switch_recommended"
    merchant_escalation = "merchant_escalation"


class FallbackMethod(str, enum.Enum):
    llm = "llm"
    safe_default = "safe_default"


class Mandate(Base):
    __tablename__ = "mandates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_ref: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    rail: Mapped[Rail] = mapped_column(Enum(Rail, name="rail_enum"), nullable=False)
    amount: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Amount in paise, matching Razorpay's smallest-unit convention"
    )
    status: Mapped[MandateStatus] = mapped_column(
        Enum(MandateStatus, name="mandate_status_enum"),
        nullable=False,
        default=MandateStatus.created,
    )
    mandate_expiry: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    razorpay_token: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Set only once a real Razorpay recurring-payment token exists (the "
        "customer completed the hosted Checkout/registration-link authorization). "
        "None means this mandate is synthetic-only; the executor uses this to "
        "decide real API call vs. stub (see app/executor.py, README).",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    failure_events: Mapped[list["FailureEvent"]] = relationship(
        back_populates="mandate", cascade="all, delete-orphan"
    )


class FailureEvent(Base):
    __tablename__ = "failure_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mandate_id: Mapped[int] = mapped_column(
        ForeignKey("mandates.id"), nullable=False, index=True
    )
    taxonomy_id: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        index=True,
        comment="P1-P12 code from docs/failure_taxonomy.md",
    )
    raw_reason_text: Mapped[str] = mapped_column(Text, nullable=False)
    ground_truth_recoverable: Mapped[bool | None] = mapped_column(
        Boolean,
        nullable=True,
        comment="Only set when the Day-2 failure simulator injects a known label",
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    mandate: Mapped["Mandate"] = relationship(back_populates="failure_events")
    retry_attempts: Mapped[list["RetryAttempt"]] = relationship(
        back_populates="failure_event", cascade="all, delete-orphan"
    )
    classifications: Mapped[list["Classification"]] = relationship(
        back_populates="failure_event", cascade="all, delete-orphan"
    )
    retry_plans: Mapped[list["RetryPlan"]] = relationship(
        back_populates="failure_event", cascade="all, delete-orphan"
    )


class RetryAttempt(Base):
    __tablename__ = "retry_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    failure_event_id: Mapped[int] = mapped_column(
        ForeignKey("failure_events.id"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[RetryOutcome] = mapped_column(
        Enum(RetryOutcome, name="retry_outcome_enum"),
        nullable=False,
        default=RetryOutcome.pending,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(128), nullable=False, unique=True, index=True
    )

    failure_event: Mapped["FailureEvent"] = relationship(back_populates="retry_attempts")


class Classification(Base):
    __tablename__ = "classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    failure_event_id: Mapped[int] = mapped_column(
        ForeignKey("failure_events.id"), nullable=False, index=True
    )
    method: Mapped[ClassificationMethod] = mapped_column(
        Enum(ClassificationMethod, name="classification_method_enum"), nullable=False
    )
    recoverable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    suggested_max_attempts: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="Set only when a fallback path caps retries (e.g. S1: LLM output "
        "invalid -> ambiguous, eligible for exactly one cautious retry)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    failure_event: Mapped["FailureEvent"] = relationship(back_populates="classifications")


class RetryPlan(Base):
    __tablename__ = "retry_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    failure_event_id: Mapped[int] = mapped_column(
        ForeignKey("failure_events.id"), nullable=False, index=True
    )
    classification_id: Mapped[int] = mapped_column(
        ForeignKey("classifications.id"), nullable=False, index=True
    )
    decision: Mapped[PlanDecision] = mapped_column(
        Enum(PlanDecision, name="plan_decision_enum"), nullable=False
    )
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    expected_value: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
        comment="Total expected value in paise for a 'retry' decision; null for 'escalate'",
    )
    escalation_reason_code: Mapped[EscalationReasonCode | None] = mapped_column(
        Enum(EscalationReasonCode, name="escalation_reason_code_enum"),
        nullable=True,
        comment="Set only for decision='escalate'; app.fallback reads this instead of "
        "parsing the free-text reasoning",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    failure_event: Mapped["FailureEvent"] = relationship(back_populates="retry_plans")
    steps: Mapped[list["PlannedAttempt"]] = relationship(
        back_populates="retry_plan",
        cascade="all, delete-orphan",
        order_by="PlannedAttempt.attempt_number",
    )
    fallback_messages: Mapped[list["FallbackMessage"]] = relationship(
        back_populates="retry_plan", cascade="all, delete-orphan"
    )


class PlannedAttempt(Base):
    __tablename__ = "planned_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    retry_plan_id: Mapped[int] = mapped_column(
        ForeignKey("retry_plans.id"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="1-indexed TOTAL attempt count for the mandate cycle, matching "
        "app.constraints' convention (1 = the original failed debit)",
    )
    proposed_timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    implied_notification_timestamp: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment="proposed_timestamp - 24h; the planner asserts it will trigger this "
        "notification, not that one already happened",
    )
    success_probability: Mapped[float] = mapped_column(Float, nullable=False)
    cost: Mapped[float] = mapped_column(Float, nullable=False, comment="Hand-specified cost in paise")
    constraint_reason: Mapped[str] = mapped_column(
        Text, nullable=False, comment="The allow reason returned by constraints.check_retry"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    retry_plan: Mapped["RetryPlan"] = relationship(back_populates="steps")


class FallbackMessage(Base):
    __tablename__ = "fallback_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    retry_plan_id: Mapped[int] = mapped_column(
        ForeignKey("retry_plans.id"), nullable=False, index=True
    )
    escalation_type: Mapped[EscalationType] = mapped_column(
        Enum(EscalationType, name="escalation_type_enum"), nullable=False
    )
    template_key: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="Which template was actually rendered -- matches escalation_type unless "
        "validation failed, in which case it's the safe-default template",
    )
    method: Mapped[FallbackMethod] = mapped_column(
        Enum(FallbackMethod, name="fallback_method_enum"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    validation_passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    validation_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    retry_plan: Mapped["RetryPlan"] = relationship(back_populates="fallback_messages")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    related_entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    related_entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
