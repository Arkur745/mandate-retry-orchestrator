"""Executor: carries out a RetryPlan's PlannedAttempts against Razorpay.

Three system-failure modes from docs/failure_taxonomy.md are real, tested
behavior here, not comments:

  S3 (retry storm / duplicate scheduling) -- schedule_retry_plan() inserts
     one retry_attempts row per PlannedAttempt with a deterministic
     idempotency_key derived from (mandate_id, failure_event_id,
     attempt_number). If called twice for the same plan, the second
     pass's inserts collide on the unique constraint (already in the
     schema since Day 1) -- caught, logged as a no-op, never a duplicate
     row or a crash.

  S4 (double-charge race) -- claim_and_execute() transitions a
     retry_attempts row from pending -> executing via a single
     conditional UPDATE ... WHERE outcome='pending', and checks rowcount.
     This is the real mechanism SQLite supports for row-level locking:
     SQLite serializes all write transactions against a given database
     (one writer commits at a time, even across separate connections/
     threads/processes), so a conditional UPDATE's rowcount is a genuine,
     atomic "did I win the claim" signal -- there is no window where two
     callers can both see rowcount=1. This is distinct from S3: S3 guards
     against the same attempt being *scheduled* (inserted) twice; S4
     guards against an *already-scheduled* row being *executed* twice by
     concurrent callers racing to claim it.

  S5 (API layer resilience) -- _call_with_backoff() wraps the actual
     Razorpay call with exponential backoff + jitter, capped at
     MAX_API_RETRIES. Explicitly logged as `api_retry` audit_log events,
     separate from retry_attempts rows -- this is API-transport retry,
     not mandate-level retry, and must never be conflated with or counted
     against the Day-5 plan's own attempt budget.

Time scaling: a single Clock abstraction (see Clock below) is used both
to decide how long APScheduler should really wait before firing a job,
and to stamp `executed_at` on completion. Fast-forward mode changes the
Clock's `scale`; every other line of execution logic -- idempotency,
locking, backoff, real-vs-stub dispatch -- is identical regardless of
scale. There is no separate demo-only implementation.
"""
from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import settings
from app.models import AuditLog, Mandate, RetryAttempt, RetryOutcome, RetryPlan

# S5: small, explicit cap -- distinct from and never counted against the
# Day-5 plan's own attempt budget (that's enforced by app.constraints).
MAX_API_RETRIES = 3
# Demo-scale base delay. A production deployment would likely use a larger
# base (1-2s) to give transient issues more room to clear; kept small here
# so tests/demos don't stall, and documented as a deliberate choice, not
# an oversight.
BASE_BACKOFF_SECONDS = 0.5


def idempotency_key_for(mandate_id: int, failure_event_id: int, attempt_number: int) -> str:
    """Deterministic, not random -- this IS the S3 guard. Two calls for the
    same logical attempt always produce the same key, so the unique
    constraint on retry_attempts.idempotency_key catches the duplicate."""
    return f"mandate:{mandate_id}:failure_event:{failure_event_id}:attempt:{attempt_number}"


@dataclass
class Clock:
    """Injectable time source. scale=1.0 is real-time; scale>1.0 compresses
    elapsed wall-clock time by that factor (e.g. scale=3600 means 1 real
    second passes as 1 simulated hour). Used identically by the scheduler
    (to compute how long to really wait) and by the executor (to stamp
    executed_at) -- one abstraction, not two code paths."""

    scale: float = 1.0
    sim_epoch: datetime = field(default_factory=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    real_epoch: float = field(default_factory=time.monotonic)

    def now(self) -> datetime:
        elapsed_real = time.monotonic() - self.real_epoch
        return self.sim_epoch + timedelta(seconds=elapsed_real * self.scale)

    def real_delay_for(self, target_sim_time: datetime) -> float:
        """Real seconds from now until target_sim_time is reached, at this
        clock's scale. At scale=1.0 this is just the literal wait; at
        scale>1.0 it's compressed by that factor."""
        sim_delta_seconds = (target_sim_time - self.now()).total_seconds()
        return max(0.0, sim_delta_seconds / self.scale)


def default_clock() -> Clock:
    return Clock(scale=settings.time_scale)


class _TransientAPIError(Exception):
    """5xx/429-shaped failure -- retryable with backoff."""


class _PermanentAPIError(Exception):
    """4xx-shaped failure that isn't 429 -- not worth retrying."""


class _APIRetriesExhausted(Exception):
    pass


def _stub_debit_call(mandate: Mandate) -> dict:
    """Shaped like a real Razorpay payment response, explicitly marked
    stub=True for auditability. Used for every mandate without a
    completed real razorpay_token (i.e. all synthetic seed mandates)."""
    return {
        "id": f"pay_stub_{uuid.uuid4().hex[:14]}",
        "status": "captured",
        "amount": mandate.amount,
        "currency": "INR",
        "method": mandate.rail.value,
        "stub": True,
    }


def _real_debit_call(razorpay_client, mandate: Mandate) -> dict:
    """Best-effort real Razorpay call for a mandate holding a completed
    token. NOT exhaustively verified end-to-end: as of this build, no
    mandate in this project's seed data has a completed real token (the
    Day-2 registration links were generated but never clicked through --
    verified by querying their invoice status directly; all three still
    read status=issued, payment_id=None). This path is structurally
    correct and ready, but unexercised against a real authorized mandate.
    """
    import razorpay.errors

    try:
        return razorpay_client.payment.create(
            {
                "amount": mandate.amount,
                "currency": "INR",
                "customer_id": mandate.customer_ref,
                "token": mandate.razorpay_token,
                "recurring": "1",
            }
        )
    except (razorpay.errors.ServerError, razorpay.errors.GatewayError) as exc:
        raise _TransientAPIError(str(exc)) from exc
    except razorpay.errors.BadRequestError as exc:
        raise _PermanentAPIError(str(exc)) from exc


def _call_with_backoff(
    call_fn: Callable[[], dict],
    *,
    db: Session,
    retry_attempt_id: int,
    max_retries: int = MAX_API_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> dict:
    rng = rng or random
    for attempt in range(1, max_retries + 1):
        try:
            return call_fn()
        except _TransientAPIError as exc:
            if attempt == max_retries:
                raise _APIRetriesExhausted(
                    f"Razorpay API failed after {max_retries} attempts: {exc}"
                ) from exc
            delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)) + rng.uniform(0, BASE_BACKOFF_SECONDS)
            db.add(
                AuditLog(
                    related_entity_type="retry_attempt",
                    related_entity_id=retry_attempt_id,
                    event_type="api_retry",
                    detail={
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "delay_seconds": round(delay, 3),
                        "error": str(exc),
                    },
                )
            )
            db.commit()
            sleep_fn(delay)
    raise _APIRetriesExhausted("unreachable")  # pragma: no cover


def schedule_retry_plan(db: Session, retry_plan: RetryPlan) -> list[RetryAttempt]:
    """S3: pre-create one retry_attempts row per PlannedAttempt, each
    pending with a deterministic idempotency key. Safe to call twice for
    the same plan -- the second pass's inserts collide and are logged as
    no-ops, returning the original rows unchanged."""
    mandate_id = retry_plan.failure_event.mandate_id
    rows: list[RetryAttempt] = []

    for step in retry_plan.steps:
        key = idempotency_key_for(mandate_id, retry_plan.failure_event_id, step.attempt_number)
        try:
            with db.begin_nested():
                row = RetryAttempt(
                    failure_event_id=retry_plan.failure_event_id,
                    attempt_number=step.attempt_number,
                    scheduled_at=step.proposed_timestamp,
                    outcome=RetryOutcome.pending,
                    idempotency_key=key,
                )
                db.add(row)
                db.flush()
            rows.append(row)
        except IntegrityError:
            existing = db.query(RetryAttempt).filter_by(idempotency_key=key).one()
            db.add(
                AuditLog(
                    related_entity_type="retry_attempt",
                    related_entity_id=existing.id,
                    event_type="duplicate_schedule_no_op",
                    detail={
                        "idempotency_key": key,
                        "reason": "retry_attempts row already exists for this "
                        "(mandate, failure_event, attempt_number) -- S3 no-op",
                    },
                )
            )
            rows.append(existing)

    db.commit()
    return rows


def claim_and_execute(
    db: Session,
    retry_attempt_id: int,
    mandate: Mandate,
    *,
    clock: Clock | None = None,
    razorpay_client=None,
    max_api_retries: int = MAX_API_RETRIES,
    sleep_fn: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> RetryAttempt:
    """S4: atomically claim the row (pending -> executing) before doing
    any work. If the claim fails (rowcount 0), someone else already has
    it -- log a no-op and return, never execute twice."""
    clock = clock or default_clock()

    claim = db.execute(
        update(RetryAttempt)
        .where(RetryAttempt.id == retry_attempt_id, RetryAttempt.outcome == RetryOutcome.pending)
        .values(outcome=RetryOutcome.executing)
    )
    db.commit()

    if claim.rowcount == 0:
        row = db.get(RetryAttempt, retry_attempt_id)
        db.add(
            AuditLog(
                related_entity_type="retry_attempt",
                related_entity_id=retry_attempt_id,
                event_type="concurrent_execution_no_op",
                detail={
                    "reason": "row was not in 'pending' state when claim was attempted "
                    f"(currently {row.outcome.value}) -- S4 no-op, not a double-execution",
                },
            )
        )
        db.commit()
        return row

    row = db.get(RetryAttempt, retry_attempt_id)
    use_real_api = mandate.razorpay_token is not None

    def call_fn() -> dict:
        if use_real_api:
            return _real_debit_call(razorpay_client, mandate)
        return _stub_debit_call(mandate)

    try:
        response = _call_with_backoff(
            call_fn,
            db=db,
            retry_attempt_id=retry_attempt_id,
            max_retries=max_api_retries,
            sleep_fn=sleep_fn,
            rng=rng,
        )
        row.outcome = RetryOutcome.success
        row.executed_at = clock.now()
        detail = {"outcome": "success", "used_real_api": use_real_api, "razorpay_response": response}
    except (_APIRetriesExhausted, _PermanentAPIError) as exc:
        row.outcome = RetryOutcome.failed
        row.executed_at = clock.now()
        detail = {"outcome": "failed", "used_real_api": use_real_api, "reason": str(exc)}

    db.add(
        AuditLog(
            related_entity_type="retry_attempt",
            related_entity_id=row.id,
            event_type="retry_attempt_executed",
            detail=detail,
        )
    )
    db.commit()
    db.refresh(row)
    return row


class RetryScheduler:
    """Wraps APScheduler to actually fire each retry_attempts row at its
    scheduled_at, translated through `clock` into a real wall-clock
    run_date. APScheduler itself only ever deals in real datetimes (it has
    no concept of simulated time) -- Clock.real_delay_for() is the single
    place that translation happens, so the job that fires and the work it
    does (claim_and_execute) are identical at any time scale. There is no
    parallel "demo mode" executor.
    """

    def __init__(
        self,
        session_factory: sessionmaker,
        *,
        clock: Clock | None = None,
        razorpay_client=None,
    ):
        self.session_factory = session_factory
        self.clock = clock or default_clock()
        self.razorpay_client = razorpay_client
        self._scheduler = BackgroundScheduler(timezone="UTC")

    def start(self) -> None:
        self._scheduler.start()

    def shutdown(self, wait: bool = True) -> None:
        self._scheduler.shutdown(wait=wait)

    def schedule_attempt(self, retry_attempt: RetryAttempt, mandate_id: int) -> str:
        """Schedule one retry_attempts row to execute at its scheduled_at,
        via the real system clock offset by clock.real_delay_for()."""
        delay_seconds = self.clock.real_delay_for(retry_attempt.scheduled_at)
        run_date = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        job_id = f"retry-attempt-{retry_attempt.id}"
        self._scheduler.add_job(
            self._run,
            trigger="date",
            run_date=run_date,
            args=[retry_attempt.id, mandate_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=None,
        )
        return job_id

    def schedule_plan(self, retry_attempts: list[RetryAttempt], mandate_id: int) -> list[str]:
        return [self.schedule_attempt(row, mandate_id) for row in retry_attempts]

    def _run(self, retry_attempt_id: int, mandate_id: int) -> None:
        db = self.session_factory()
        try:
            mandate = db.get(Mandate, mandate_id)
            claim_and_execute(
                db,
                retry_attempt_id,
                mandate,
                clock=self.clock,
                razorpay_client=self.razorpay_client,
            )
        finally:
            db.close()
