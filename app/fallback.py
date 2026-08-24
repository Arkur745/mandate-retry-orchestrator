"""Fallback agent: the second and last place an LLM (Groq) enters this
pipeline. Only ever runs on a planner decision of 'escalate'.

Design choice worth stating up front: **template selection is
deterministic code, not an LLM decision.** Sending a customer the wrong
*category* of message (e.g. "your mandate needs re-authorization" when the
real issue is a merchant-side rail problem they can't act on) is a
high-stakes error, and the mapping from (taxonomy_id, escalation reason,
rail) to template is already fully known from data this module already
has -- there's no genuine ambiguity to resolve, unlike app.classifier's
P3/P12 cases. This mirrors the project's established principle (see
docs/build_schedule.md's panel-prep notes) of using the LLM only where a
decision is genuinely ambiguous, not for things code can determine
reliably. So: `determine_escalation_type()` below is pure code. The LLM's
job, exactly as scoped, is to fill in the natural-language slots of the
*already-selected* template (phrasing, not category selection) -- facts
like the amount are injected by code, never generated, which also removes
the main hallucination surface for those fields entirely.

S8 (inappropriate fallback message): the LLM's slot-fill output is
validated before use -- correct slot keys, plausible length, and no
cross-category "grounding" leakage (the generated text mentioning a
failure category other than the one that's actually true). Any validation
failure -- or a malformed/unparseable LLM response, mirroring S1's
philosophy in app.classifier -- falls back to a fixed, hardcoded
safe-default message with zero LLM-generated content, logged to
audit_log. The pipeline never ships an unvalidated message and never
crashes on a bad LLM response.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.db import settings
from app.models import (
    AuditLog,
    EscalationReasonCode,
    EscalationType,
    FailureEvent,
    FallbackMessage,
    FallbackMethod,
    Mandate,
    Rail,
    RetryPlan,
)
from app.planner import CATEGORY_PROFILES

# Human-readable, customer/merchant-safe description per taxonomy_id --
# used as the *only* source of truth the LLM is given for "what happened",
# so grounding validation can check its output doesn't drift from this.
CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "P1": "insufficient balance in the customer's account at debit time",
    "P2": "a temporary issuing bank or NPCI timeout",
    "P3": "a risk/fraud hold placed by the issuing bank",
    "P4": "the mandate has expired",
    "P5": "the mandate was revoked or paused by the customer",
    "P6": "the mandate's per-cycle debit limit was exceeded",
    "P7": "the pre-debit notification window was not acknowledged in time",
    "P8": "the card on file has expired or was blocked",
    "P9": "a temporary issuing bank outage affecting card e-mandate execution",
    "P10": "a normal e-NACH physical clearing cycle delay",
    "P11": "the customer's account is frozen pending KYC re-verification",
    "P12": "an unclassified or ambiguous decline from the issuer",
}

# Terms distinctive enough to a category that seeing them in generated
# text for a *different* category is a grounding violation (S8). Kept
# deliberately small/explicit rather than exhaustive -- false positives
# (blocking legitimate phrasing) are cheap to fix by trimming a term;
# false negatives (missing a real hallucination) are the actual risk this
# guards against.
CATEGORY_SIGNAL_TERMS: dict[str, tuple[str, ...]] = {
    "P1": ("insufficient balance", "insufficient funds"),
    "P2": ("timed out", "timeout"),
    "P3": ("fraud", "risk hold", "suspicious activity"),
    "P4": ("mandate has expired", "mandate expired"),
    "P5": ("revoked", "cancelled by you", "paused by you"),
    "P6": ("debit limit", "exceeds your mandate limit"),
    "P7": ("notification was not acknowledged", "notification window"),
    "P8": ("card has expired", "card was blocked", "card expired"),
    "P9": ("bank outage", "issuer unavailable"),
    "P10": ("clearing cycle", "business days to clear"),
    "P11": ("frozen", "kyc"),
    "P12": ("unclassified", "unable to determine"),
}


def _check_grounding(text: str, actual_taxonomy_id: str) -> str | None:
    """Returns an error string if `text` contains a signal term belonging
    to a DIFFERENT category than the one that actually happened -- e.g.
    generated copy for a P4 (expired) event that mentions "fraud" (P3) or
    "insufficient funds" (P1). Returns None if clean."""
    lowered = text.lower()
    own_terms = set(CATEGORY_SIGNAL_TERMS.get(actual_taxonomy_id, ()))
    for other_id, terms in CATEGORY_SIGNAL_TERMS.items():
        if other_id == actual_taxonomy_id:
            continue
        for term in terms:
            if term in own_terms:
                continue
            if term in lowered:
                return (
                    f"generated text contains {term!r}, a signal term for category "
                    f"{other_id!r}, but the actual category is {actual_taxonomy_id!r}"
                )
    return None


class ValidationError(Exception):
    pass


@dataclass(frozen=True)
class TemplateSpec:
    key: EscalationType
    customer_facing: bool
    slot_keys: tuple[str, ...]
    min_length: int
    max_length: int
    render: Callable[..., str]
    safe_default_text: str


def _render_retry_exhausted(*, amount_display: str, customer_explanation: str, next_step_instruction: str) -> str:
    return (
        f"We couldn't automatically complete your payment of {amount_display}. "
        f"{customer_explanation} {next_step_instruction}"
    )


def _render_reauth_needed(*, amount_display: str, reason_explanation: str, action_instruction: str) -> str:
    return (
        f"Your recurring payment of {amount_display} could not be processed. "
        f"{reason_explanation} {action_instruction}"
    )


def _render_rail_switch(*, mandate_id: int, rail: str, recommended_rail: str, structural_reasoning: str) -> str:
    return (
        f"Mandate {mandate_id} ({rail}) escalated: {structural_reasoning} "
        f"Recommended action: switch this customer to {recommended_rail} for retries."
    )


def _render_merchant_escalation(*, mandate_id: int, category_description: str, summary: str) -> str:
    return f"Mandate {mandate_id} requires manual review ({category_description}). {summary}"


TEMPLATES: dict[EscalationType, TemplateSpec] = {
    EscalationType.retry_exhausted_nudge: TemplateSpec(
        key=EscalationType.retry_exhausted_nudge,
        customer_facing=True,
        slot_keys=("customer_explanation", "next_step_instruction"),
        min_length=10,
        max_length=200,
        render=_render_retry_exhausted,
        safe_default_text=(
            "We were unable to process your recent payment. Please complete "
            "your payment using the secure link we've sent you, or contact "
            "support if you need help."
        ),
    ),
    EscalationType.reauth_needed: TemplateSpec(
        key=EscalationType.reauth_needed,
        customer_facing=True,
        slot_keys=("reason_explanation", "action_instruction"),
        min_length=10,
        max_length=200,
        render=_render_reauth_needed,
        safe_default_text=(
            "We were unable to process your recurring payment because your "
            "payment method needs to be updated. Please visit your account "
            "settings to set up a new mandate."
        ),
    ),
    EscalationType.rail_switch_recommended: TemplateSpec(
        key=EscalationType.rail_switch_recommended,
        customer_facing=False,
        slot_keys=("structural_reasoning",),
        min_length=10,
        max_length=400,
        render=_render_rail_switch,
        safe_default_text=(
            "This mandate's payment rail could not sustain the retry timing "
            "required for this failure category under current operational "
            "constraints. Manual review recommended -- consider an alternate "
            "payment rail for this customer."
        ),
    ),
    EscalationType.merchant_escalation: TemplateSpec(
        key=EscalationType.merchant_escalation,
        customer_facing=False,
        slot_keys=("summary",),
        min_length=5,
        max_length=400,
        render=_render_merchant_escalation,
        safe_default_text=(
            "This mandate requires manual review. Automated fallback message "
            "generation failed validation -- see audit_log for detail."
        ),
    ),
}

# Categories where "not_recoverable" means the customer's payment method
# itself needs updating/re-authorizing (as opposed to a merchant-side
# issue the customer can't act on).
_REAUTH_TAXONOMY_IDS = frozenset({"P4", "P5", "P8"})


def determine_escalation_type(
    failure_event: FailureEvent, retry_plan: RetryPlan, mandate: Mandate
) -> EscalationType:
    """Pure code, no LLM -- see module docstring for why."""
    code = retry_plan.escalation_reason_code

    if code == EscalationReasonCode.all_candidates_vetoed:
        profile = CATEGORY_PROFILES.get(failure_event.taxonomy_id)
        # The structural case: a fast-retry-shaped category (P2, P9) on a
        # rail whose minimum inter-attempt spacing floor (Day 4) is wider
        # than the profile's fastest offsets, so every candidate is vetoed
        # regardless of timing choice -- this isn't "bad luck", it's the
        # rail being unsuitable for this category's recovery profile. Not
        # literally hardcoded to P9: any category using fast_technical on
        # a non-UPI rail hits the identical structural wall (see Day 5/6
        # report -- P9 is simply the case that's ALWAYS true, since it's
        # card_emandate-only).
        if profile is not None and profile.name == "fast_technical" and mandate.rail != Rail.upi_autopay:
            return EscalationType.rail_switch_recommended
        return EscalationType.retry_exhausted_nudge

    if code in (EscalationReasonCode.low_confidence, EscalationReasonCode.negative_expected_value):
        return EscalationType.retry_exhausted_nudge

    if code == EscalationReasonCode.no_timing_profile:
        return EscalationType.merchant_escalation

    if code == EscalationReasonCode.not_recoverable:
        if failure_event.taxonomy_id in _REAUTH_TAXONOMY_IDS:
            return EscalationType.reauth_needed
        return EscalationType.merchant_escalation

    raise ValueError(f"Unhandled escalation_reason_code: {code!r}")


def _default_groq_client():
    import groq

    return groq.Groq(api_key=settings.groq_api_key)


def _amount_display(mandate: Mandate) -> str:
    return f"Rs {mandate.amount / 100:,.2f}"


def _build_prompt(template: TemplateSpec, failure_event: FailureEvent, retry_plan: RetryPlan, mandate: Mandate) -> tuple[str, str]:
    category_description = CATEGORY_DESCRIPTIONS.get(failure_event.taxonomy_id, "an unspecified issue")
    audience = "a customer" if template.customer_facing else "the merchant's operations team"
    system = (
        f"You are drafting a short notification for {audience} about a payment mandate "
        "that could not be automatically retried. Respond with ONLY a JSON object "
        f"containing EXACTLY these keys, each a short plain-language phrase (no markdown, "
        f"no extra keys, no amounts or account numbers -- those are handled separately): "
        f"{list(template.slot_keys)}. "
        f"The ONLY reason for this escalation is: {category_description}. "
        "Do not mention, imply, or speculate about any other reason."
    )
    user = (
        f"Rail: {mandate.rail.value}\n"
        f"Planner's escalation reasoning: \"{retry_plan.reasoning}\""
    )
    return system, user


def _validate_and_parse(raw: str, template: TemplateSpec, taxonomy_id: str) -> dict[str, str]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValidationError(f"not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValidationError("response is not a JSON object")

    if set(data.keys()) != set(template.slot_keys):
        raise ValidationError(
            f"expected exactly keys {sorted(template.slot_keys)}, got {sorted(data.keys())}"
        )

    for key in template.slot_keys:
        value = data[key]
        if not isinstance(value, str):
            raise ValidationError(f"slot {key!r} must be a string, got {value!r}")
        if not (template.min_length <= len(value) <= template.max_length):
            raise ValidationError(
                f"slot {key!r} length {len(value)} outside [{template.min_length}, "
                f"{template.max_length}]"
            )
        grounding_error = _check_grounding(value, taxonomy_id)
        if grounding_error:
            raise ValidationError(grounding_error)

    return data


def generate_fallback_message(
    db: Session,
    retry_plan: RetryPlan,
    failure_event: FailureEvent,
    mandate: Mandate,
    *,
    groq_client: Any = None,
    commit: bool = True,
) -> FallbackMessage:
    if retry_plan.decision.value != "escalate":
        raise ValueError("generate_fallback_message only runs on an 'escalate' RetryPlan")

    escalation_type = determine_escalation_type(failure_event, retry_plan, mandate)
    template = TEMPLATES[escalation_type]

    try:
        client = groq_client if groq_client is not None else _default_groq_client()
        system, user = _build_prompt(template, failure_event, retry_plan, mandate)
        response = client.chat.completions.create(
            model=settings.groq_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.3,
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content
        slots = _validate_and_parse(raw, template, failure_event.taxonomy_id)

        if escalation_type == EscalationType.rail_switch_recommended:
            content = template.render(
                mandate_id=mandate.id,
                rail=mandate.rail.value,
                recommended_rail=Rail.upi_autopay.value,
                **slots,
            )
        elif escalation_type == EscalationType.merchant_escalation:
            content = template.render(
                mandate_id=mandate.id,
                category_description=CATEGORY_DESCRIPTIONS.get(failure_event.taxonomy_id, "unspecified"),
                **slots,
            )
        else:
            content = template.render(amount_display=_amount_display(mandate), **slots)

        message = FallbackMessage(
            retry_plan_id=retry_plan.id,
            escalation_type=escalation_type,
            template_key=escalation_type.value,
            method=FallbackMethod.llm,
            content=content,
            validation_passed=True,
            validation_detail=None,
        )
    except Exception as exc:  # noqa: BLE001 - S8: never let a bad LLM response ship or crash
        db.add(
            AuditLog(
                related_entity_type="retry_plan",
                related_entity_id=retry_plan.id,
                event_type="fallback_safe_default_used",
                detail={"escalation_type": escalation_type.value, "error": str(exc)},
            )
        )
        message = FallbackMessage(
            retry_plan_id=retry_plan.id,
            escalation_type=escalation_type,
            template_key=f"{escalation_type.value}_safe_default",
            method=FallbackMethod.safe_default,
            content=template.safe_default_text,
            validation_passed=False,
            validation_detail=str(exc),
        )

    db.add(message)
    if commit:
        db.commit()
        db.refresh(message)
    else:
        db.flush()
    return message
