"""Failure classifier: reproduces the recoverability judgment already
encoded in docs/failure_taxonomy.md Part 1 — it does not invent new logic.

Two paths:

1. Rule filter (zero LLM calls) -- covers every category with a single,
   unambiguous "Retry-worthy" answer in the taxonomy table: P1, P2, P4, P5,
   P6, P8, P9, P10, P11. Also covers P7, whose answer is "Conditional" on
   whether the pre-debit notification window is still open -- resolved
   deterministically from the decline text rather than an LLM judgment call,
   since the taxonomy already specifies the exact rule ("retry only within
   valid notification window, else treat as hard fail").

2. LLM path (Groq) -- covers only the two categories the taxonomy itself
   flags as genuinely ambiguous: P12 ("classifier escalates to LLM",
   explicit in the doc) and P3 (risk/fraud hold -- "cautious yes" in
   principle, but bank-side and uncertain). Requires strict JSON output.

S1 fallback (built now, not deferred): any LLM response that fails to parse
as JSON, or whose `recoverable` field isn't true/false/null, is treated as
the conservative default -- ambiguous, eligible for exactly one cautious
retry -- and the fallback firing is logged to audit_log. The pipeline never
crashes or raises on a bad LLM response.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.db import settings
from app.models import (
    AuditLog,
    Classification,
    ClassificationMethod,
    FailureEvent,
    Mandate,
)

# Categories with one fixed answer, straight from the taxonomy's
# "Retry-worthy" column. P3, P7, P12 are handled separately below.
RULE_TABLE: dict[str, bool] = {
    "P1": True,
    "P2": True,
    "P4": False,
    "P5": False,
    "P6": False,
    "P8": False,
    "P9": True,
    "P10": True,
    "P11": False,
}

# The only two categories the taxonomy treats as genuinely ambiguous.
LLM_CATEGORIES = frozenset({"P3", "P12"})

_SYSTEM_PROMPT = (
    "You are a payment-mandate failure classifier for Indian recurring "
    "payments (UPI Autopay, e-NACH, card e-mandate). Given a raw bank/NPCI "
    "decline reason and mandate context, judge whether retrying the debit "
    "is likely to recover the payment. Respond with ONLY a single JSON "
    'object, no markdown, no extra text: {"recoverable": true|false|null, '
    '"confidence": <number 0.0-1.0>, "reasoning": "<one short sentence>"}. '
    "Use null for recoverable only if it is genuinely undeterminable from "
    "the given information."
)


class LLMResponseError(ValueError):
    """Raised internally when a Groq response fails to parse/validate.
    Never escapes classify_failure() -- it's the S1 trigger condition."""


def _default_groq_client():
    import groq

    return groq.Groq(api_key=settings.groq_api_key)


def _p7_recoverable_from_text(raw_reason_text: str) -> bool:
    """P7's rule is deterministic per the taxonomy, conditioned on whether
    the notification-acknowledgement window is still open. The simulator
    (app/simulator.py) renders exactly one of two variants that make this
    determinable from the text; a real system would check a stored
    notification timestamp instead of parsing text, but this is the only
    signal available at this schema layer."""
    return "window is still open" in raw_reason_text or "window still open" in raw_reason_text


def _prior_failure_count(db: Session, mandate_id: int, before_event_id: int) -> int:
    return (
        db.query(FailureEvent)
        .filter(FailureEvent.mandate_id == mandate_id, FailureEvent.id < before_event_id)
        .count()
    )


def _call_groq(client: Any, *, rail: str, prior_failure_count: int, raw_reason_text: str) -> str:
    user_content = (
        f"Rail: {rail}\n"
        f"Prior failure events on this mandate: {prior_failure_count}\n"
        f'Raw decline reason from bank/NPCI: "{raw_reason_text}"'
    )
    response = client.chat.completions.create(
        model=settings.groq_model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        temperature=0.2,
        max_tokens=300,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content


def _parse_llm_response(raw: str) -> tuple[bool | None, float | None, str | None]:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMResponseError(f"not valid JSON: {exc}") from exc

    if not isinstance(data, dict) or "recoverable" not in data:
        raise LLMResponseError("missing 'recoverable' field")

    recoverable = data["recoverable"]
    if recoverable is not None and not isinstance(recoverable, bool):
        raise LLMResponseError(f"'recoverable' must be true/false/null, got {recoverable!r}")

    confidence = data.get("confidence")
    if confidence is not None and not isinstance(confidence, (int, float)):
        raise LLMResponseError(f"'confidence' must be numeric, got {confidence!r}")

    reasoning = data.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, str):
        raise LLMResponseError(f"'reasoning' must be a string, got {reasoning!r}")

    return recoverable, confidence, reasoning


@dataclass
class _Decision:
    method: ClassificationMethod
    recoverable: bool | None
    confidence: float | None
    reasoning: str | None
    suggested_max_attempts: int | None


def _classify_via_llm(
    db: Session, event: FailureEvent, mandate: Mandate, groq_client: Any
) -> _Decision:
    try:
        client = groq_client if groq_client is not None else _default_groq_client()
        prior_count = _prior_failure_count(db, mandate.id, event.id)
        raw = _call_groq(
            client,
            rail=mandate.rail.value,
            prior_failure_count=prior_count,
            raw_reason_text=event.raw_reason_text,
        )
        recoverable, confidence, reasoning = _parse_llm_response(raw)
        return _Decision(
            method=ClassificationMethod.llm,
            recoverable=recoverable,
            confidence=confidence,
            reasoning=reasoning,
            suggested_max_attempts=None,
        )
    except Exception as exc:  # noqa: BLE001 - S1: never let a bad LLM call crash the pipeline
        db.add(
            AuditLog(
                related_entity_type="failure_event",
                related_entity_id=event.id,
                event_type="classifier_llm_fallback",
                detail={"taxonomy_id": event.taxonomy_id, "error": str(exc)},
            )
        )
        return _Decision(
            method=ClassificationMethod.llm_fallback,
            recoverable=None,
            confidence=None,
            reasoning=f"S1 fallback: LLM output invalid/unparseable ({exc}); "
            "treating as ambiguous, eligible for exactly one cautious retry.",
            suggested_max_attempts=1,
        )


def classify_failure(
    db: Session,
    event: FailureEvent,
    mandate: Mandate,
    *,
    groq_client: Any = None,
    commit: bool = True,
) -> Classification:
    """Classify one failure_events row. Never calls Groq for a rule-path
    category, even if `groq_client` is provided."""
    if event.taxonomy_id in RULE_TABLE:
        decision = _Decision(
            method=ClassificationMethod.rule,
            recoverable=RULE_TABLE[event.taxonomy_id],
            confidence=1.0,
            reasoning=f"Rule lookup: {event.taxonomy_id} is "
            f"{'retry-worthy' if RULE_TABLE[event.taxonomy_id] else 'not retry-worthy'} "
            "per docs/failure_taxonomy.md.",
            suggested_max_attempts=None,
        )
    elif event.taxonomy_id == "P7":
        recoverable = _p7_recoverable_from_text(event.raw_reason_text)
        decision = _Decision(
            method=ClassificationMethod.rule,
            recoverable=recoverable,
            confidence=1.0,
            reasoning="Rule lookup: P7 is conditional on the pre-debit notification "
            f"window; resolved from decline text as {'still open' if recoverable else 'expired'}.",
            suggested_max_attempts=None,
        )
    elif event.taxonomy_id in LLM_CATEGORIES:
        decision = _classify_via_llm(db, event, mandate, groq_client)
    else:
        raise ValueError(f"Unknown taxonomy_id: {event.taxonomy_id!r}")

    classification = Classification(
        failure_event_id=event.id,
        method=decision.method,
        recoverable=decision.recoverable,
        confidence=decision.confidence,
        reasoning=decision.reasoning,
        suggested_max_attempts=decision.suggested_max_attempts,
    )
    db.add(classification)
    if commit:
        db.commit()
        db.refresh(classification)
    else:
        db.flush()
    return classification
