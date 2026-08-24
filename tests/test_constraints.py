"""Tests for app/constraints.py -- the hard veto layer.

One test per rule proving both the allow and veto side, plus explicit
proof that a mandate on one rail is never evaluated against another
rail's rules.
"""
from datetime import datetime, timedelta

import pytest

from app.constraints import AFA_EXEMPT_THRESHOLD_PAISE, check_retry
from app.models import Mandate, MandateStatus, Rail

# A day picked arbitrarily but consistent across tests.
DAY = datetime(2026, 8, 24)


def ist(hour: int, minute: int = 0) -> datetime:
    """Naive-UTC datetime corresponding to the given IST wall-clock time
    on DAY (matches app.constraints._to_ist's fixed +5:30 convention)."""
    return DAY + timedelta(hours=hour, minutes=minute) - timedelta(hours=5, minutes=30)


def make_mandate(rail: Rail, amount: int = 99900, expiry: datetime = datetime(2027, 1, 1)) -> Mandate:
    return Mandate(
        id=1,
        customer_ref="cust_1",
        rail=rail,
        amount=amount,
        status=MandateStatus.active,
        mandate_expiry=expiry,
    )


# ---- UPI Autopay: max 4 total attempts ----------------------------------


def test_upi_attempt_4_allowed_attempt_5_vetoed():
    mandate = make_mandate(Rail.upi_autopay)
    ts = ist(15, 0)  # inside non-peak window
    notif = ts - timedelta(hours=48)

    ok = check_retry(mandate, 4, ts, last_notification_at=notif)
    assert ok.allowed is True

    blocked = check_retry(mandate, 5, ts, last_notification_at=notif)
    assert blocked.allowed is False
    assert "4-attempt cap" in blocked.reason


# ---- UPI Autopay: non-peak windows --------------------------------------


def test_upi_timestamp_in_window_allowed_out_of_window_vetoed():
    mandate = make_mandate(Rail.upi_autopay)
    notif_base = DAY - timedelta(days=2)

    in_window = ist(15, 0)  # 15:00 IST, inside 13:00-17:00
    ok = check_retry(mandate, 2, in_window, last_notification_at=notif_base)
    assert ok.allowed is True

    out_of_window = ist(11, 0)  # 11:00 IST, inside blocked 10:00-13:00
    blocked = check_retry(mandate, 2, out_of_window, last_notification_at=notif_base)
    assert blocked.allowed is False
    assert "peak window" in blocked.reason


def test_upi_overnight_window_wraps_midnight():
    mandate = make_mandate(Rail.upi_autopay)
    notif_base = DAY - timedelta(days=2)

    late_night = ist(23, 0)  # 23:00 IST, inside >=21:30
    assert check_retry(mandate, 2, late_night, last_notification_at=notif_base).allowed is True

    early_morning = ist(5, 0)  # 05:00 IST, inside <10:00
    assert check_retry(mandate, 2, early_morning, last_notification_at=notif_base).allowed is True


# ---- UPI Autopay: >=24h pre-debit notification ---------------------------


def test_upi_notification_24h_prior_allowed_sooner_vetoed():
    mandate = make_mandate(Rail.upi_autopay)
    ts = ist(15, 0)

    ok = check_retry(mandate, 2, ts, last_notification_at=ts - timedelta(hours=25))
    assert ok.allowed is True

    blocked = check_retry(mandate, 2, ts, last_notification_at=ts - timedelta(hours=5))
    assert blocked.allowed is False
    assert "pre-debit notification" in blocked.reason or "notification" in blocked.reason


def test_upi_missing_notification_timestamp_fails_closed():
    mandate = make_mandate(Rail.upi_autopay)
    ts = ist(15, 0)
    result = check_retry(mandate, 2, ts)
    assert result.allowed is False
    assert "cannot confirm" in result.reason.lower()


# ---- Universal: mandate expiry -------------------------------------------


def test_pre_expiry_allowed_post_expiry_vetoed():
    expiry = datetime(2026, 9, 1)
    mandate = make_mandate(Rail.e_nach, expiry=expiry)

    before = expiry - timedelta(days=1)
    ok = check_retry(mandate, 1, before)
    assert ok.allowed is True

    after = expiry + timedelta(days=1)
    blocked = check_retry(mandate, 1, after)
    assert blocked.allowed is False
    assert "expiry" in blocked.reason


# ---- e-NACH / card e-mandate: max 3 total attempts -----------------------


@pytest.mark.parametrize("rail", [Rail.e_nach, Rail.card_emandate])
def test_generic_rail_attempt_3_allowed_attempt_4_vetoed(rail):
    mandate = make_mandate(rail)
    ts = DAY
    prev = ts - timedelta(hours=48)

    ok = check_retry(mandate, 3, ts, previous_attempt_at=prev)
    assert ok.allowed is True

    blocked = check_retry(mandate, 4, ts, previous_attempt_at=prev)
    assert blocked.allowed is False
    assert "3-attempt cap" in blocked.reason


# ---- e-NACH / card e-mandate: >=24h spacing between attempts -------------


@pytest.mark.parametrize("rail", [Rail.e_nach, Rail.card_emandate])
def test_generic_rail_spacing_24h_allowed_sooner_vetoed(rail):
    mandate = make_mandate(rail)
    ts = DAY

    ok = check_retry(mandate, 2, ts, previous_attempt_at=ts - timedelta(hours=25))
    assert ok.allowed is True

    blocked = check_retry(mandate, 2, ts, previous_attempt_at=ts - timedelta(hours=5))
    assert blocked.allowed is False
    assert "spacing" in blocked.reason


def test_generic_rail_first_attempt_does_not_need_previous_attempt_at():
    mandate = make_mandate(Rail.e_nach)
    result = check_retry(mandate, 1, DAY)
    assert result.allowed is True


def test_generic_rail_missing_previous_attempt_at_fails_closed_for_retry():
    mandate = make_mandate(Rail.card_emandate)
    result = check_retry(mandate, 2, DAY)
    assert result.allowed is False
    assert "cannot confirm" in result.reason.lower()


# ---- Universal: AFA warning (informational, never a veto) ----------------


def test_high_amount_gets_afa_warning_but_is_still_allowed():
    mandate = make_mandate(Rail.e_nach, amount=AFA_EXEMPT_THRESHOLD_PAISE + 100)
    ts = DAY
    result = check_retry(mandate, 1, ts)
    assert result.allowed is True
    assert any("AFA" in w or "authentication" in w for w in result.warnings)


def test_low_amount_gets_no_afa_warning():
    mandate = make_mandate(Rail.e_nach, amount=AFA_EXEMPT_THRESHOLD_PAISE - 100)
    result = check_retry(mandate, 1, DAY)
    assert result.allowed is True
    assert result.warnings == []


# ---- Cross-rail isolation -------------------------------------------------


def test_channel_mismatch_is_vetoed_regardless_of_other_params():
    mandate = make_mandate(Rail.upi_autopay)
    ts = ist(15, 0)
    result = check_retry(mandate, 1, ts, channel=Rail.e_nach, last_notification_at=ts - timedelta(hours=48))
    assert result.allowed is False
    assert "does not match" in result.reason


def test_upi_attempt_cap_does_not_apply_to_other_rails():
    # Attempt 4 is within UPI's cap but would exceed e_nach/card's cap of 3.
    upi_mandate = make_mandate(Rail.upi_autopay)
    e_nach_mandate = make_mandate(Rail.e_nach)
    ts = ist(15, 0)
    notif = ts - timedelta(hours=48)
    prev = ts - timedelta(hours=48)

    upi_result = check_retry(upi_mandate, 4, ts, last_notification_at=notif)
    assert upi_result.allowed is True

    e_nach_result = check_retry(e_nach_mandate, 4, ts, previous_attempt_at=prev)
    assert e_nach_result.allowed is False
    assert "3-attempt cap" in e_nach_result.reason


def test_non_peak_window_rule_does_not_apply_to_e_nach_or_card():
    # 11:00 IST is a blocked peak window for UPI, but e_nach/card don't
    # enforce non-peak windows at all -- same timestamp must be allowed.
    e_nach_mandate = make_mandate(Rail.e_nach)
    peak_ts = ist(11, 0)
    result = check_retry(e_nach_mandate, 1, peak_ts)
    assert result.allowed is True


def test_notification_rule_does_not_apply_to_e_nach_or_card():
    # e_nach/card have no notification_hours rule at all -- omitting
    # last_notification_at must not fail closed the way it does for UPI.
    card_mandate = make_mandate(Rail.card_emandate)
    result = check_retry(card_mandate, 1, DAY)
    assert result.allowed is True


def test_invalid_attempt_number_raises():
    mandate = make_mandate(Rail.upi_autopay)
    with pytest.raises(ValueError):
        check_retry(mandate, 0, DAY)
