"""Tests for app/executor.py -- S3 (idempotency), S4 (concurrency), S5
(API backoff), S9 (audit log write failure), and time-scaled scheduling.
"""
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
import razorpay.errors
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.executor import (
    Clock,
    RetryScheduler,
    _call_with_backoff,
    _TransientAPIError,
    claim_and_execute,
    idempotency_key_for,
    schedule_retry_plan,
)
from app.models import (
    AuditLog,
    Base,
    Classification,
    ClassificationMethod,
    FailureEvent,
    Mandate,
    MandateStatus,
    PlanDecision,
    Rail,
    RetryAttempt,
    RetryOutcome,
    RetryPlan,
)
from app.planner import plan_retries

OCCURRED_AT = datetime(2026, 8, 24, 9, 0, 0)  # 14:30 IST


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


def make_mandate(db: Session, rail: Rail = Rail.upi_autopay, **overrides) -> Mandate:
    defaults = dict(
        customer_ref="cust_1",
        rail=rail,
        amount=99900,
        status=MandateStatus.active,
        mandate_expiry=datetime(2027, 1, 1),
    )
    defaults.update(overrides)
    mandate = Mandate(**defaults)
    db.add(mandate)
    db.commit()
    db.refresh(mandate)
    return mandate


def make_mandate_and_event(db: Session, rail: Rail = Rail.card_emandate, taxonomy_id: str = "P9"):
    """For tests that only need a mandate + failure_event to hang a
    RetryAttempt off directly -- doesn't go through the planner, so it
    works even for taxonomy/rail combinations (like P9/card_emandate)
    where the planner always escalates (see the P9 structural-veto finding
    reported alongside this module)."""
    mandate = make_mandate(db, rail)
    event = FailureEvent(
        mandate_id=mandate.id, taxonomy_id=taxonomy_id, raw_reason_text="x", occurred_at=OCCURRED_AT
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return mandate, event


def make_planned_retry(db: Session, rail: Rail = Rail.upi_autopay, taxonomy_id: str = "P2") -> RetryPlan:
    mandate = make_mandate(db, rail)
    event = FailureEvent(
        mandate_id=mandate.id, taxonomy_id=taxonomy_id, raw_reason_text="x", occurred_at=OCCURRED_AT
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    cls = Classification(
        failure_event_id=event.id,
        method=ClassificationMethod.rule,
        recoverable=True,
        confidence=1.0,
        reasoning="rule",
    )
    db.add(cls)
    db.commit()
    db.refresh(cls)
    plan = plan_retries(db, event, cls, mandate)
    assert plan.decision == PlanDecision.retry
    return mandate, plan


# ---- S3: idempotent double scheduling ------------------------------------


def test_idempotency_key_is_deterministic():
    a = idempotency_key_for(1, 2, 3)
    b = idempotency_key_for(1, 2, 3)
    assert a == b
    assert a != idempotency_key_for(1, 2, 4)


def test_double_scheduling_is_a_noop_not_a_duplicate_or_crash(db: Session):
    mandate, plan = make_planned_retry(db)

    first = schedule_retry_plan(db, plan)
    total_after_first = db.query(RetryAttempt).count()

    second = schedule_retry_plan(db, plan)  # must not raise
    total_after_second = db.query(RetryAttempt).count()

    assert total_after_first == total_after_second == len(plan.steps)
    assert [r.id for r in first] == [r.id for r in second]

    noop_logs = db.query(AuditLog).filter_by(event_type="duplicate_schedule_no_op").all()
    assert len(noop_logs) == len(plan.steps)


# ---- S4: real concurrency race, not a sequential call ---------------------


def test_concurrent_claim_only_one_execution_wins(tmp_path):
    db_path = tmp_path / "concurrency_test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_busy_timeout(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()

    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)

    setup_db = SessionFactory()
    mandate = make_mandate(setup_db)
    event_row = FailureEvent(
        mandate_id=mandate.id, taxonomy_id="P2", raw_reason_text="x", occurred_at=OCCURRED_AT
    )
    setup_db.add(event_row)
    setup_db.commit()
    retry_attempt = RetryAttempt(
        failure_event_id=event_row.id,
        attempt_number=2,
        scheduled_at=OCCURRED_AT + timedelta(hours=1),
        outcome=RetryOutcome.pending,
        idempotency_key=idempotency_key_for(mandate.id, event_row.id, 2),
    )
    setup_db.add(retry_attempt)
    setup_db.commit()
    row_id = retry_attempt.id
    mandate_id = mandate.id
    setup_db.close()

    N_WORKERS = 5
    barrier = threading.Barrier(N_WORKERS)
    outcomes: list[RetryOutcome] = []
    lock = threading.Lock()

    def worker():
        worker_db = SessionFactory()
        try:
            m = worker_db.get(Mandate, mandate_id)
            barrier.wait(timeout=5)  # line every thread up to race as hard as possible
            result = claim_and_execute(worker_db, row_id, m, clock=Clock(scale=1.0))
            with lock:
                outcomes.append(result.outcome)
        finally:
            worker_db.close()

    threads = [threading.Thread(target=worker) for _ in range(N_WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(outcomes) == N_WORKERS  # every thread returned, none crashed/hung
    # Every thread's returned outcome reads 'success' here -- including the
    # no-op callers, since a no-op legitimately returns the row's *current*
    # state (already settled to success by the winning thread), not an
    # error. That's correct behavior, not evidence of a double-execution.
    # The real proof that only one execution happened is the audit trail:
    # exactly one retry_attempt_executed log, and N_WORKERS-1 explicit
    # concurrent_execution_no_op logs from the callers that lost the race.
    assert all(o == RetryOutcome.success for o in outcomes)

    verify_db = SessionFactory()
    try:
        executed_logs = verify_db.query(AuditLog).filter_by(event_type="retry_attempt_executed").all()
        noop_logs = verify_db.query(AuditLog).filter_by(event_type="concurrent_execution_no_op").all()
        assert len(executed_logs) == 1
        assert len(noop_logs) == N_WORKERS - 1
        final_row = verify_db.get(RetryAttempt, row_id)
        assert final_row.outcome == RetryOutcome.success
    finally:
        verify_db.close()


# ---- S5: API backoff on simulated 5xx/429 ---------------------------------


def test_backoff_retries_then_succeeds(db: Session):
    mandate, plan = make_planned_retry(db)
    rows = schedule_retry_plan(db, plan)

    calls = {"n": 0}

    def flaky_call() -> dict:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _TransientAPIError(f"simulated 503 (attempt {calls['n']})")
        return {"id": "pay_ok", "status": "captured"}

    sleeps: list[float] = []
    result = _call_with_backoff(
        flaky_call,
        db=db,
        retry_attempt_id=rows[0].id,
        max_retries=5,
        sleep_fn=sleeps.append,
        rng=__import__("random").Random(0),
    )

    assert result == {"id": "pay_ok", "status": "captured"}
    assert calls["n"] == 3
    assert len(sleeps) == 2  # backed off twice, succeeded on the 3rd try
    retry_logs = db.query(AuditLog).filter_by(event_type="api_retry", related_entity_id=rows[0].id).all()
    assert len(retry_logs) == 2
    assert retry_logs[0].detail["attempt"] == 1
    assert retry_logs[1].detail["attempt"] == 2


def test_backoff_gives_up_cleanly_after_cap_without_crashing(db: Session):
    mandate, plan = make_planned_retry(db)
    rows = schedule_retry_plan(db, plan)

    def always_fails() -> dict:
        raise _TransientAPIError("simulated 429 rate limit")

    sleeps: list[float] = []
    with pytest.raises(Exception):  # _APIRetriesExhausted, not a raw crash
        _call_with_backoff(
            always_fails,
            db=db,
            retry_attempt_id=rows[0].id,
            max_retries=3,
            sleep_fn=sleeps.append,
            rng=__import__("random").Random(0),
        )
    assert len(sleeps) == 2  # backs off between attempts 1->2 and 2->3, then gives up


def test_claim_and_execute_with_mocked_real_api_backoff_then_success(db: Session):
    mandate, event_row = make_mandate_and_event(db)
    mandate.razorpay_token = "token_test123"
    db.commit()

    row = RetryAttempt(
        failure_event_id=event_row.id,
        attempt_number=2,
        scheduled_at=OCCURRED_AT + timedelta(hours=1),
        outcome=RetryOutcome.pending,
        idempotency_key=idempotency_key_for(mandate.id, event_row.id, 2),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    mock_client = MagicMock()
    mock_client.payment.create.side_effect = [
        razorpay.errors.ServerError("simulated 5xx"),
        {"id": "pay_real_ok", "status": "captured"},
    ]

    result = claim_and_execute(
        db, row.id, mandate, clock=Clock(scale=1.0), razorpay_client=mock_client, sleep_fn=lambda s: None
    )

    assert result.outcome == RetryOutcome.success
    assert mock_client.payment.create.call_count == 2
    retry_logs = db.query(AuditLog).filter_by(event_type="api_retry", related_entity_id=row.id).all()
    assert len(retry_logs) == 1


def test_claim_and_execute_fails_cleanly_when_real_api_exhausts_retries(db: Session):
    mandate, event_row = make_mandate_and_event(db)
    mandate.razorpay_token = "token_test123"
    db.commit()

    row = RetryAttempt(
        failure_event_id=event_row.id,
        attempt_number=2,
        scheduled_at=OCCURRED_AT + timedelta(hours=1),
        outcome=RetryOutcome.pending,
        idempotency_key=idempotency_key_for(mandate.id, event_row.id, 2),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    mock_client = MagicMock()
    mock_client.payment.create.side_effect = razorpay.errors.GatewayError("simulated 429 always")

    result = claim_and_execute(  # must not raise/crash
        db, row.id, mandate, clock=Clock(scale=1.0), razorpay_client=mock_client, sleep_fn=lambda s: None
    )

    assert result.outcome == RetryOutcome.failed
    assert mock_client.payment.create.call_count == 3  # MAX_API_RETRIES


# ---- Fast-forward vs real-time: same code path, same outcome -------------


def test_fast_forward_and_real_time_clocks_produce_identical_outcomes(db: Session):
    mandate_rt, plan_rt = make_planned_retry(db, taxonomy_id="P2")
    rows_rt = schedule_retry_plan(db, plan_rt)
    result_rt = claim_and_execute(db, rows_rt[0].id, mandate_rt, clock=Clock(scale=1.0))

    mandate_ff, plan_ff = make_planned_retry(db, taxonomy_id="P2")
    rows_ff = schedule_retry_plan(db, plan_ff)
    result_ff = claim_and_execute(db, rows_ff[0].id, mandate_ff, clock=Clock(scale=100_000.0))

    assert result_rt.outcome == result_ff.outcome == RetryOutcome.success
    detail_rt = (
        db.query(AuditLog)
        .filter_by(event_type="retry_attempt_executed", related_entity_id=result_rt.id)
        .one()
        .detail
    )
    detail_ff = (
        db.query(AuditLog)
        .filter_by(event_type="retry_attempt_executed", related_entity_id=result_ff.id)
        .one()
        .detail
    )
    assert detail_rt["outcome"] == detail_ff["outcome"] == "success"
    assert detail_rt["used_real_api"] == detail_ff["used_real_api"] is False


def test_clock_real_delay_scales_correctly():
    clock = Clock(scale=3600.0)  # 1 real second = 1 simulated hour
    target = clock.now() + timedelta(hours=2)
    delay = clock.real_delay_for(target)
    assert 1.9 < delay < 2.1  # ~2 real seconds for 2 simulated hours


# ---- Scheduler wiring: actually fires a job via APScheduler ---------------


def test_scheduler_fires_and_executes_job(db: Session):
    engine = db.get_bind()
    SessionFactory = sessionmaker(bind=engine)
    mandate, plan = make_planned_retry(db, taxonomy_id="P2")
    rows = schedule_retry_plan(db, plan)

    # Fast-forward heavily so the plan's real offsets (minutes-hours out)
    # collapse to a fraction of a real second.
    clock = Clock(scale=1_000_000.0)
    scheduler = RetryScheduler(SessionFactory, clock=clock)
    scheduler.start()
    try:
        scheduler.schedule_attempt(rows[0], mandate.id)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            db.expire_all()
            row = db.get(RetryAttempt, rows[0].id)
            if row.outcome != RetryOutcome.pending:
                break
            time.sleep(0.05)
        else:
            pytest.fail("scheduled job did not fire within the timeout")
    finally:
        scheduler.shutdown(wait=True)

    db.expire_all()
    final_row = db.get(RetryAttempt, rows[0].id)
    assert final_row.outcome == RetryOutcome.success


# ---- Day 9 Part C: S9 -- a real DB write failure during audit logging ----
#
# Not a mocked Python exception in isolation -- a SQLAlchemy
# before_cursor_execute hook that intercepts the actual SQL statement
# reaching the DBAPI and raises a genuine sqlite3.OperationalError only
# for INSERT INTO audit_log, leaving every other statement untouched.
# This simulates what a real transient DB failure (disk I/O error, lock
# timeout) hitting specifically the audit-log write would look like.


def _make_audit_log_failing_engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "before_cursor_execute")
    def _fail_audit_log_inserts(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().startswith("INSERT INTO audit_log"):
            raise sqlite3.OperationalError("simulated disk I/O error writing audit_log")

    return engine


def test_audit_log_write_failure_fails_loudly_not_silently(tmp_path):
    engine = _make_audit_log_failing_engine()
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    db = SessionFactory()

    mandate = make_mandate(db)
    event_row = FailureEvent(
        mandate_id=mandate.id, taxonomy_id="P2", raw_reason_text="x", occurred_at=OCCURRED_AT
    )
    db.add(event_row)
    db.commit()
    retry_attempt = RetryAttempt(
        failure_event_id=event_row.id,
        attempt_number=2,
        scheduled_at=OCCURRED_AT + timedelta(hours=1),
        outcome=RetryOutcome.pending,
        idempotency_key=idempotency_key_for(mandate.id, event_row.id, 2),
    )
    db.add(retry_attempt)
    db.commit()
    row_id = retry_attempt.id

    # The pipeline must not silently continue past the unlogged step --
    # it must fail loudly (raise), not return a row claiming success/failed
    # with no audit trail to explain what happened.
    with pytest.raises(sqlite3.OperationalError):
        claim_and_execute(db, row_id, mandate, clock=Clock(scale=1.0))

    db.rollback()
    row = db.get(RetryAttempt, row_id)
    # Never a false "success" shipped without its audit entry -- the
    # state-change commit (which would have set success/failed) rolled
    # back atomically together with the failed audit_log insert.
    assert row.outcome != RetryOutcome.success
    assert row.outcome != RetryOutcome.failed
    assert db.query(AuditLog).count() == 0


def test_audit_log_write_failure_leaves_no_misleading_audit_trail(tmp_path):
    # Companion check: confirm there is no PARTIAL audit_log row either
    # (e.g. a half-written entry) -- the failed insert leaves zero rows,
    # not a corrupt one.
    engine = _make_audit_log_failing_engine()
    Base.metadata.create_all(engine)
    SessionFactory = sessionmaker(bind=engine)
    db = SessionFactory()

    mandate = make_mandate(db)
    event_row = FailureEvent(
        mandate_id=mandate.id, taxonomy_id="P2", raw_reason_text="x", occurred_at=OCCURRED_AT
    )
    db.add(event_row)
    db.commit()
    retry_attempt = RetryAttempt(
        failure_event_id=event_row.id,
        attempt_number=2,
        scheduled_at=OCCURRED_AT + timedelta(hours=1),
        outcome=RetryOutcome.pending,
        idempotency_key=idempotency_key_for(mandate.id, event_row.id, 2),
    )
    db.add(retry_attempt)
    db.commit()

    with pytest.raises(sqlite3.OperationalError):
        claim_and_execute(db, retry_attempt.id, mandate, clock=Clock(scale=1.0))

    db.rollback()
    assert db.query(AuditLog).count() == 0
