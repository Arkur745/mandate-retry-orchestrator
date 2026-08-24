"""Retry planner: deterministic, cost-based. No LLM anywhere in this module
-- if a change here ever needs an LLM call, that's a sign the change belongs
in app/classifier.py instead.

Given a failure_events row, its classifications row, and the mandate, this
module outputs one of two first-class decisions:

  - "retry": an ordered sequence of proposed attempts (timestamp + implied
    notification timestamp), chosen by searching a small, explicit set of
    candidate timing offsets and picking the sequence that maximizes total
    expected value, with every step pre-validated against
    app.constraints.check_retry.
  - "escalate": do not retry. This is not a null/fallback branch -- it's
    reached explicitly (recoverable=False, low confidence, every candidate
    sequence vetoed, or the best available sequence has non-positive
    expected value per S7 in docs/failure_taxonomy.md) and always carries
    its own specific reasoning.

All probability/cost numbers below are hand-specified, not fit from data,
per the project's own scoping decision -- each has a one-line rationale
comment. None of them claim regulatory authority the way app/constraints.py
does; they're planning-model assumptions, and are labeled as such.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.constraints import check_retry
from app.models import (
    Classification,
    FailureEvent,
    Mandate,
    PlanDecision,
    PlannedAttempt,
    RetryPlan,
)

# Below this classifier confidence, a "recoverable" verdict isn't trusted
# enough to spend retry budget on. 0.5 = the LLM expressing less certainty
# than a coin flip; rule-path classifications are always confidence=1.0 so
# this only ever gates the LLM path (P3/P12). Missing confidence (None) is
# treated as failing this check -- fail closed, consistent with
# app.constraints' treatment of missing compliance facts.
CONFIDENCE_THRESHOLD = 0.5

# A "retry" plan whose best candidate sequence has expected value at or
# below this is worse than doing nothing -- escalate instead (S7: planner
# must be able to output "do not retry" as a first-class action, not just
# retry sequences). Zero, not a small positive number: any sequence with
# positive expected value is at least nominally worth proposing; the
# planner isn't trying to hit a profit margin, just avoid negative-EV plans.
EV_ESCALATE_THRESHOLD_PAISE = 0.0

# Cost model: increases with attempt position to stand in for bank-flagging
# risk, NPCI/issuer rate-limit consumption, and customer friction, none of
# which are precisely quantifiable, so this is a simple, explicit,
# monotonically increasing curve rather than a fitted one.
# BASE_COST: a nominal operational cost for the first retry -- small
# relative to typical mandate amounts, so it doesn't dominate EV for
# ordinary transaction sizes, but still makes later attempts in a sequence
# comparable rather than free.
BASE_COST_PAISE = 200.0
# COST_GROWTH: each subsequent attempt is 60% more "expensive" than the
# last -- risk of triggering bank/NPCI abuse flags compounds with attempt
# count, it doesn't stay flat.
COST_GROWTH = 1.6


def _cost(step_index: int) -> float:
    """step_index is 0-indexed position within the retry sequence."""
    return BASE_COST_PAISE * (COST_GROWTH**step_index)


@dataclass(frozen=True)
class TimingProfile:
    name: str
    # One tuple of candidate hour-offsets (from failure_event.occurred_at)
    # per slot; slot i's candidates are all explored as "sequence includes
    # a step in slot i". Sequences only use consecutive slots starting at
    # slot 0 (no skipping the fast retry to go straight to a slow one).
    slot_offsets_hours: tuple[tuple[float, ...], ...]
    base_probability: float
    decay_per_step: float
    max_attempts_override: int | None
    rationale: str


# Probability is modeled as base_probability * (decay_per_step ** step_index)
# -- decays across the sequence for every category (per spec), varying by
# category only in how fast it decays and where in time the candidate
# offsets sit. We deliberately do NOT model different success odds for
# different offset choices *within* the same slot (e.g. retrying at 0.5h
# vs 1h) -- there's no data-driven basis to distinguish them at this
# scope, so probability depends on sequence position only; offset choice
# within a slot affects constraint feasibility and cost timing, not
# probability.

_FAST_TECHNICAL = TimingProfile(
    name="fast_technical",
    slot_offsets_hours=((0.5, 1.0), (2.0, 4.0), (8.0, 24.0)),
    base_probability=0.60,
    decay_per_step=0.65,
    max_attempts_override=None,
    rationale=(
        "P2/P9 are transient technical failures (bank/NPCI timeout, issuer "
        "downtime). docs/failure_taxonomy.md's own strategy column says "
        "'Short-delay retry, 1-2 quick attempts before backing off' -- so "
        "offsets are sub-day and probability decays steeply (0.65/step): "
        "if a fast retry doesn't fix it, the issue probably isn't the "
        "momentary blip the fast-retry strategy assumes."
    ),
)

_DELAYED_FUNDS = TimingProfile(
    name="delayed_funds",
    slot_offsets_hours=((24.0,), (72.0,), (168.0,)),
    base_probability=0.40,
    decay_per_step=0.85,
    max_attempts_override=None,
    rationale=(
        "P1 (insufficient balance) clusters near salary cycles per the "
        "taxonomy doc. 24h/72h/168h (1/3/7 days) roughly matches common "
        "subscription-billing dunning cadences (e.g. Stripe/Chargebee/"
        "Recurly-style recovery schedules for insufficient-funds declines) "
        "-- an industry-observed pattern, NOT a regulation, unlike "
        "app.constraints' cited rules. Decay is slow (0.85/step) since "
        "funds-timing issues resolve gradually over days, not within hours "
        "like a technical failure."
    ),
)

_CAUTIOUS_SINGLE = TimingProfile(
    name="cautious_single",
    slot_offsets_hours=((48.0,),),
    base_probability=0.45,
    decay_per_step=1.0,  # irrelevant: only one slot ever exists
    max_attempts_override=1,
    rationale=(
        "P3 (risk/fraud hold) and P12 (ambiguous/unclassified) are the two "
        "categories the taxonomy doc itself flags as genuinely uncertain. "
        "P3's strategy column is explicit: 'Single delayed retry only; "
        "repeated attempts risk mandate flagging.' Capped at exactly one "
        "attempt regardless of classification.suggested_max_attempts, at "
        "a moderate 48h delay and moderate (not high) base probability, "
        "reflecting genuine uncertainty rather than confidence either way."
    ),
)

_CLEARING_CYCLE = TimingProfile(
    name="clearing_cycle",
    slot_offsets_hours=((24.0, 48.0), (48.0, 72.0)),
    base_probability=0.65,
    decay_per_step=0.90,
    max_attempts_override=None,
    rationale=(
        "P10 (e-NACH physical clearing delay) is 'semi-transient' per the "
        "taxonomy doc, with a stated 1-3 business day recovery window -- "
        "offsets are taken directly from that table entry. Higher base "
        "probability and slow decay (0.90/step) reflect that this isn't "
        "really a failure so much as normal clearing latency; it usually "
        "resolves on its own timeline."
    ),
)

_NOTIFICATION_WINDOW = TimingProfile(
    name="notification_window",
    slot_offsets_hours=((1.0,),),
    base_probability=0.55,
    decay_per_step=1.0,
    max_attempts_override=1,
    rationale=(
        "P7 (pre-debit notification not acknowledged) is only 'recoverable' "
        "at all when app.classifier's rule resolved the notification window "
        "as still open -- retrying must happen soon, within that window, "
        "not days later. Single attempt at 1h, moderate probability."
    ),
)

CATEGORY_PROFILES: dict[str, TimingProfile] = {
    "P1": _DELAYED_FUNDS,
    "P2": _FAST_TECHNICAL,
    "P3": _CAUTIOUS_SINGLE,
    "P7": _NOTIFICATION_WINDOW,
    "P9": _FAST_TECHNICAL,
    "P10": _CLEARING_CYCLE,
    "P12": _CAUTIOUS_SINGLE,  # same treatment as P3: genuinely uncertain, one cautious retry.
}


@dataclass(frozen=True)
class _Candidate:
    offsets_hours: tuple[float, ...]
    timestamps: tuple[datetime, ...]
    probabilities: tuple[float, ...]
    costs: tuple[float, ...]
    constraint_reasons: tuple[str, ...]
    expected_value: float


def _search_candidates(
    profile: TimingProfile,
    failure_event: FailureEvent,
    mandate: Mandate,
    max_attempts: int,
) -> list[_Candidate]:
    """Every valid (constraint-passing) candidate sequence, of every length
    from 1 up to max_attempts, exploring each slot's candidate offsets.
    Returns them all (not just the best) so callers/tests/scripts can
    inspect the full search, not just the winner."""
    usable_slots = profile.slot_offsets_hours[:max_attempts]
    valid: list[_Candidate] = []

    for length in range(1, len(usable_slots) + 1):
        for combo in itertools.product(*usable_slots[:length]):
            if any(combo[i] >= combo[i + 1] for i in range(len(combo) - 1)):
                continue  # offsets must strictly increase across the sequence

            timestamps = tuple(
                failure_event.occurred_at + timedelta(hours=h) for h in combo
            )

            prev_at = failure_event.occurred_at
            reasons: list[str] = []
            all_allowed = True
            for step_index, ts in enumerate(timestamps):
                attempt_number = step_index + 2  # occurred_at's original debit is attempt 1
                last_notification_at = ts - timedelta(hours=24)
                result = check_retry(
                    mandate,
                    attempt_number,
                    ts,
                    channel=mandate.rail,
                    last_notification_at=last_notification_at,
                    previous_attempt_at=prev_at,
                )
                if not result.allowed:
                    all_allowed = False
                    break
                reasons.append(result.reason)
                prev_at = ts

            if not all_allowed:
                continue

            probabilities = tuple(
                profile.base_probability * (profile.decay_per_step**i) for i in range(length)
            )
            costs = tuple(_cost(i) for i in range(length))
            ev = sum(p * mandate.amount - c for p, c in zip(probabilities, costs))

            valid.append(
                _Candidate(
                    offsets_hours=combo,
                    timestamps=timestamps,
                    probabilities=probabilities,
                    costs=costs,
                    constraint_reasons=tuple(reasons),
                    expected_value=ev,
                )
            )

    return valid


def plan_retries(
    db: Session,
    failure_event: FailureEvent,
    classification: Classification,
    mandate: Mandate,
    *,
    commit: bool = True,
) -> RetryPlan:
    """Produce a RetryPlan: either 'retry' with an ordered PlannedAttempt
    sequence, or 'escalate' with a specific reason. Every early-exit branch
    below runs before any constraint-store call, so a doomed case (not
    recoverable, low confidence) never wastes a search."""

    if classification.recoverable is not True:
        return _escalate(
            db,
            failure_event,
            classification,
            reasoning=(
                f"Classification recoverable={classification.recoverable!r} "
                f"(method={classification.method.value}) -- not confidently "
                "recoverable, no search attempted."
            ),
            commit=commit,
        )

    if classification.confidence is None or classification.confidence < CONFIDENCE_THRESHOLD:
        return _escalate(
            db,
            failure_event,
            classification,
            reasoning=(
                f"Classification confidence ({classification.confidence!r}) is below "
                f"the {CONFIDENCE_THRESHOLD} retry threshold -- too uncertain to spend "
                "retry budget on, no search attempted."
            ),
            commit=commit,
        )

    profile = CATEGORY_PROFILES.get(failure_event.taxonomy_id)
    if profile is None:
        return _escalate(
            db,
            failure_event,
            classification,
            reasoning=(
                f"No timing profile defined for category {failure_event.taxonomy_id!r} "
                "despite recoverable=True -- escalating defensively rather than "
                "guessing a search space."
            ),
            commit=commit,
        )

    max_attempts = len(profile.slot_offsets_hours)
    if profile.max_attempts_override is not None:
        max_attempts = min(max_attempts, profile.max_attempts_override)
    if classification.suggested_max_attempts is not None:
        max_attempts = min(max_attempts, classification.suggested_max_attempts)

    candidates = _search_candidates(profile, failure_event, mandate, max_attempts)

    if not candidates:
        return _escalate(
            db,
            failure_event,
            classification,
            reasoning=(
                f"Every candidate sequence in the {profile.name} search space "
                f"({sum(len(list(itertools.product(*profile.slot_offsets_hours[:L]))) for L in range(1, max_attempts + 1))} "
                "combinations considered) was vetoed by the constraint store -- "
                "no valid retry timing exists (e.g. mandate expiring imminently)."
            ),
            commit=commit,
        )

    best = max(candidates, key=lambda c: c.expected_value)

    if best.expected_value <= EV_ESCALATE_THRESHOLD_PAISE:
        return _escalate(
            db,
            failure_event,
            classification,
            reasoning=(
                f"Best candidate sequence ({profile.name}, offsets={best.offsets_hours}) "
                f"has expected value {best.expected_value:.2f} paise <= "
                f"{EV_ESCALATE_THRESHOLD_PAISE:.2f} -- not worth retrying (S7)."
            ),
            commit=commit,
        )

    plan = RetryPlan(
        failure_event_id=failure_event.id,
        classification_id=classification.id,
        decision=PlanDecision.retry,
        reasoning=(
            f"Selected {len(best.offsets_hours)}-step {profile.name} sequence "
            f"(offsets={best.offsets_hours}h) with expected value "
            f"{best.expected_value:.2f} paise, out of {len(candidates)} valid "
            f"candidate sequences evaluated. {profile.rationale}"
        ),
        expected_value=best.expected_value,
    )
    db.add(plan)
    db.flush()

    for step_index, ts in enumerate(best.timestamps):
        db.add(
            PlannedAttempt(
                retry_plan_id=plan.id,
                attempt_number=step_index + 2,
                proposed_timestamp=ts,
                implied_notification_timestamp=ts - timedelta(hours=24),
                success_probability=best.probabilities[step_index],
                cost=best.costs[step_index],
                constraint_reason=best.constraint_reasons[step_index],
            )
        )

    if commit:
        db.commit()
        db.refresh(plan)
    else:
        db.flush()
    return plan


def _escalate(
    db: Session,
    failure_event: FailureEvent,
    classification: Classification,
    *,
    reasoning: str,
    commit: bool,
) -> RetryPlan:
    plan = RetryPlan(
        failure_event_id=failure_event.id,
        classification_id=classification.id,
        decision=PlanDecision.escalate,
        reasoning=reasoning,
        expected_value=None,
    )
    db.add(plan)
    if commit:
        db.commit()
        db.refresh(plan)
    else:
        db.flush()
    return plan
