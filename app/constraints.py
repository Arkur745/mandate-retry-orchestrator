"""Constraint store: a hard veto layer over proposed retries.

This module never suggests a retry -- it only answers "is this specific
proposed attempt allowed?" for a plan the (future, Day-5) planner already
produced. It has zero dependency on app.classifier or app.simulator; it
only reads Mandate/Rail fields it already has access to, plus timing
context passed explicitly by the caller (see check_retry's keyword args).

Every rule below is either a cited regulatory source or is explicitly
labeled "OPERATIONAL ASSUMPTION" -- see docs/constraints.md for the full
writeup with sources. Do not add a rule here without one of those two
labels.

Design decision -- reject, don't auto-adjust, out-of-window timestamps:
this module vetoes a bad proposal rather than silently shifting it into
the next valid window. Auto-adjusting would mean the constraint store is
quietly re-planning, which contradicts its stated job (a veto layer, not
a suggestion engine) and the project's own S6 failure mode (see
docs/failure_taxonomy.md): "Constraint store is a hard veto layer checked
*after* planning, before execution -- plan is never trusted blindly."
Re-proposing a corrected timestamp is the planner's job, not this
module's; the planner can re-submit and get checked again.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from app.models import Mandate, Rail

IST_OFFSET = timedelta(hours=5, minutes=30)  # IST has no DST; fixed offset is exact.

# RBI's e-mandate framework AFA-exemption ceiling. Sourced (see
# docs/constraints.md): recurring digital payments up to Rs 15,000 may be
# processed without per-transaction additional factor authentication once
# an e-mandate is registered with AFA; above that, issuers may require
# fresh authentication per attempt. Amount is stored in paise (app.models
# convention), so the threshold is 15,000 * 100.
AFA_EXEMPT_THRESHOLD_PAISE = 15_000 * 100


@dataclass(frozen=True)
class ConstraintResult:
    allowed: bool
    reason: str
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RailRuleSet:
    max_total_attempts: int
    max_total_attempts_source: str
    notification_hours: int | None
    notification_hours_source: str | None
    spacing_hours: int | None
    spacing_hours_source: str | None
    enforce_non_peak_windows: bool
    non_peak_windows_source: str | None


# --- UPI Autopay: NPCI rules, effective 2025-08-01 --------------------
# Source (see docs/constraints.md for full citation trail): NPCI circular
# issued ~2025-05-21 directing PSPs/banks to moderate high-frequency APIs,
# compliance deadline 2025-07-31. Widely reported as: UPI Autopay mandate
# execution capped at 1 original + 3 retries (4 total attempts), processed
# only in non-peak windows (before 10:00, 13:00-17:00, after 21:30 IST).
_UPI_AUTOPAY_RULES = RailRuleSet(
    max_total_attempts=4,
    max_total_attempts_source=(
        "NPCI circular (~2025-05-21, compliance deadline 2025-07-31 / effective "
        "2025-08-01): UPI Autopay recurring-mandate execution capped at 1 "
        "original + 3 retries per cycle."
    ),
    notification_hours=24,
    notification_hours_source=(
        "RBI Digital Payments - E-mandate Framework (originating circular "
        "DPSS.CO.PD.No.447/02.14.003/2019-20, dated 2019-08-21; consolidated "
        "into the 2026 framework): pre-debit notification required >=24h "
        "before ANY debit attempt, original or retry."
    ),
    spacing_hours=None,
    spacing_hours_source=None,
    enforce_non_peak_windows=True,
    non_peak_windows_source=(
        "Same NPCI circular as max_total_attempts: AutoPay processing "
        "restricted to non-peak windows (before 10:00, 13:00-17:00, after "
        "21:30 IST) to reduce failure rates and ease peak-hour server load."
    ),
)

# --- e-NACH: no UPI-specific NPCI rule applies; configurable defaults --
# OPERATIONAL ASSUMPTION, not a cited regulation for this rail specifically.
# The 24h RBI e-mandate notification framework, as researched, is described
# in secondary reporting as covering cards/UPI/PPI; e-NACH bank-account
# debits historically sit under NPCI's separate NACH mandate framework, so
# applying the same 24h figure here is a reasonable default pending
# confirmation against the NACH-specific circular, not a re-verified fact.
_E_NACH_RULES = RailRuleSet(
    max_total_attempts=3,
    max_total_attempts_source=(
        "OPERATIONAL ASSUMPTION: no cited NPCI/RBI cap found for e-NACH retry "
        "count specifically. 3 total attempts chosen as a conservative default."
    ),
    notification_hours=None,
    notification_hours_source=None,
    spacing_hours=24,
    spacing_hours_source=(
        "OPERATIONAL ASSUMPTION: minimum spacing between consecutive attempts, "
        "not a cited e-NACH-specific regulation."
    ),
    enforce_non_peak_windows=False,
    non_peak_windows_source=None,
)

# --- Card e-mandate: same treatment as e-NACH ---------------------------
# OPERATIONAL ASSUMPTION. Some secondary reporting on the RBI e-mandate
# framework does describe card e-mandates as covered by the same 24h
# pre-debit notification language used for UPI; per this project's scoping
# (see docs/constraints.md) that is treated as an unverified secondary
# claim, not re-confirmed against RBI's primary text, so card e-mandate
# uses the same configurable-default treatment as e-NACH rather than
# borrowing UPI's cited figure.
_CARD_EMANDATE_RULES = RailRuleSet(
    max_total_attempts=3,
    max_total_attempts_source=(
        "OPERATIONAL ASSUMPTION: no cited NPCI/RBI cap found for card "
        "e-mandate retry count specifically. 3 total attempts chosen as a "
        "conservative default."
    ),
    notification_hours=None,
    notification_hours_source=None,
    spacing_hours=24,
    spacing_hours_source=(
        "OPERATIONAL ASSUMPTION: minimum spacing between consecutive attempts, "
        "not a re-verified card-e-mandate-specific regulation."
    ),
    enforce_non_peak_windows=False,
    non_peak_windows_source=None,
)

RAIL_RULES: dict[Rail, RailRuleSet] = {
    Rail.upi_autopay: _UPI_AUTOPAY_RULES,
    Rail.e_nach: _E_NACH_RULES,
    Rail.card_emandate: _CARD_EMANDATE_RULES,
}


def _to_ist(dt_utc: datetime) -> datetime:
    """proposed_timestamp is treated as naive UTC, matching the naive
    DateTime columns used throughout app.models. IST has no DST, so a
    fixed +5:30 offset is exact, not an approximation."""
    return dt_utc + IST_OFFSET


def _is_non_peak_ist(t: time) -> bool:
    # Non-peak: 13:00-17:00, or >=21:30, or <10:00 (wraps midnight).
    return (time(13, 0) <= t < time(17, 0)) or t >= time(21, 30) or t < time(10, 0)


def check_retry(
    mandate: Mandate,
    proposed_attempt_number: int,
    proposed_timestamp: datetime,
    channel: Rail | None = None,
    *,
    last_notification_at: datetime | None = None,
    previous_attempt_at: datetime | None = None,
) -> ConstraintResult:
    """Is this specific proposed attempt allowed?

    proposed_attempt_number is the 1-indexed TOTAL attempt count for this
    mandate cycle: 1 = the original failed debit that produced the
    failure_event, 2+ = retries. (Matches "1 original + 3 retries = 4
    total" from the NPCI rule below.)

    proposed_timestamp is naive UTC (see _to_ist).

    channel, if given, must match mandate.rail -- this both catches a
    caller bug (proposing a retry on the wrong rail) and is what the tests
    use to prove a mandate on one rail is never evaluated against another
    rail's rules.

    last_notification_at / previous_attempt_at are supplied by the caller
    (the planner has this context; this module deliberately doesn't query
    other tables for it). If a rule needs one of these and it's not
    supplied, the proposal is vetoed rather than assumed compliant --
    fail closed on a compliance-relevant fact we can't confirm.
    """
    if proposed_attempt_number < 1:
        raise ValueError(f"proposed_attempt_number must be >= 1, got {proposed_attempt_number}")

    if channel is not None and channel != mandate.rail:
        return ConstraintResult(
            allowed=False,
            reason=(
                f"Proposed retry channel {channel.value!r} does not match mandate "
                f"{mandate.id}'s registered rail {mandate.rail.value!r}."
            ),
        )

    # Universal: mandate expiry. Not itself a cited circular -- it's the
    # basic lifecycle fact that no debit authorization exists past expiry.
    if proposed_timestamp > mandate.mandate_expiry:
        return ConstraintResult(
            allowed=False,
            reason=(
                f"Proposed timestamp {proposed_timestamp.isoformat()} is after "
                f"mandate {mandate.id}'s expiry {mandate.mandate_expiry.isoformat()}. "
                "No debit authorization exists past mandate expiry."
            ),
        )

    rules = RAIL_RULES[mandate.rail]

    if proposed_attempt_number > rules.max_total_attempts:
        return ConstraintResult(
            allowed=False,
            reason=(
                f"Attempt {proposed_attempt_number} exceeds the {rules.max_total_attempts}-attempt "
                f"cap for {mandate.rail.value}. Source: {rules.max_total_attempts_source}"
            ),
        )

    if rules.enforce_non_peak_windows:
        ist_time = _to_ist(proposed_timestamp).time()
        if not _is_non_peak_ist(ist_time):
            return ConstraintResult(
                allowed=False,
                reason=(
                    f"Proposed timestamp falls at {ist_time.isoformat()} IST, inside a "
                    "peak window (10:00-13:00 or 17:00-21:30 IST). Must fall within "
                    "13:00-17:00 or 21:30-10:00 IST. "
                    f"Source: {rules.non_peak_windows_source}"
                ),
            )

    if rules.notification_hours is not None:
        if last_notification_at is None:
            return ConstraintResult(
                allowed=False,
                reason=(
                    f"No last_notification_at supplied; cannot confirm the "
                    f"{rules.notification_hours}h pre-debit notification requirement "
                    f"was met. Failing closed. Source: {rules.notification_hours_source}"
                ),
            )
        hours_since_notification = (proposed_timestamp - last_notification_at).total_seconds() / 3600
        if hours_since_notification < rules.notification_hours:
            return ConstraintResult(
                allowed=False,
                reason=(
                    f"Only {hours_since_notification:.1f}h since last pre-debit "
                    f"notification; {rules.notification_hours}h required. "
                    f"Source: {rules.notification_hours_source}"
                ),
            )

    if rules.spacing_hours is not None and proposed_attempt_number > 1:
        if previous_attempt_at is None:
            return ConstraintResult(
                allowed=False,
                reason=(
                    f"No previous_attempt_at supplied; cannot confirm the minimum "
                    f"{rules.spacing_hours}h spacing between attempts was met. "
                    f"Failing closed. Source: {rules.spacing_hours_source}"
                ),
            )
        hours_since_previous = (proposed_timestamp - previous_attempt_at).total_seconds() / 3600
        if hours_since_previous < rules.spacing_hours:
            return ConstraintResult(
                allowed=False,
                reason=(
                    f"Only {hours_since_previous:.1f}h since the previous attempt; "
                    f"{rules.spacing_hours}h minimum spacing required. "
                    f"Source: {rules.spacing_hours_source}"
                ),
            )

    warnings: list[str] = []
    if mandate.amount > AFA_EXEMPT_THRESHOLD_PAISE:
        warnings.append(
            f"Mandate amount ({mandate.amount / 100:.2f} INR) exceeds the "
            "Rs 15,000 AFA-exemption threshold; additional factor authentication "
            "may be required for this attempt, which the planner should factor "
            "into cost/latency, not treat as a block."
        )

    return ConstraintResult(
        allowed=True,
        reason=f"Attempt {proposed_attempt_number} for mandate {mandate.id} passes all {mandate.rail.value} constraints.",
        warnings=warnings,
    )
