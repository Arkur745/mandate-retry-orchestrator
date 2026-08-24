# Constraint store — rule set and sources

`app/constraints.py` is a hard veto layer: given a proposed retry (mandate,
attempt number, timestamp, channel), it answers allowed/blocked with a
specific reason. It never proposes a retry itself — that's the Day-5
planner's job. This doc exists because these are factual regulatory claims
a panel interview might reasonably probe, so each rule below is labeled
either a **cited source** or an explicit **operational assumption**.

Research method note: sources below were found via web search against
financial-press reporting on the relevant RBI/NPCI circulars, not by
fetching the primary circular text directly (NPCI circulars in particular
aren't reliably published with a stable public URL). Where a claim rests
on secondary reporting rather than a primary document I've read myself,
that's called out explicitly rather than presented as more certain than it
is.

---

## UPI Autopay (NPCI rules, effective 2025-08-01)

### Max 4 total attempts per mandate cycle (1 original + 3 retries)

**Cited.** NPCI issued a circular around 2025-05-21 directing banks and
PSPs to moderate and monitor usage of ten high-frequency APIs, with a
compliance deadline of 2025-07-31 (effective 2025-08-01). Widely reported
result: UPI Autopay recurring-mandate execution is capped at one original
attempt plus three retries per cycle — a 5th attempt is rejected outright,
regardless of what a classifier or planner proposes.

*Implementation note:* `proposed_attempt_number` is the 1-indexed **total**
attempt count for the cycle (1 = the original failed debit that produced
the `failure_event`, 2–4 = retries). Attempt 4 is allowed; attempt 5 is
vetoed.

### Non-peak processing windows: 13:00–17:00 or 21:30–10:00 IST

**Cited**, same circular as above. AutoPay transaction processing is
restricted to non-peak windows — before 10:00, between 13:00–17:00, and
after 21:30 IST — specifically to reduce failure rates and ease server
load during peak traffic. A proposed timestamp inside 10:00–13:00 or
17:00–21:30 IST is vetoed.

*Design decision — reject, don't auto-adjust:* the constraint store vetoes
an out-of-window proposal rather than silently shifting it to the next
valid window. Auto-adjusting would make this module a second planner,
which contradicts both its stated job here (pure allow/block, no
suggestions) and the project's own S6 failure mode
(`docs/failure_taxonomy.md`): *"Constraint store is a hard veto layer
checked after planning, before execution — plan is never trusted
blindly."* If this module silently rewrote timestamps, it would itself be
a second, hidden planner making retry decisions — exactly the "plan
trusted blindly" failure mode S6 exists to prevent. Re-proposing a
corrected timestamp is the planner's job; it can re-submit and get checked
again.

### Pre-debit notification ≥24h before any debit attempt

**Cited**, but from a different (RBI, not NPCI) source: RBI's Digital
Payments – E-mandate Framework, originating from circular
DPSS.CO.PD.No.447/02.14.003/2019-20 (2019-08-21) and consolidated into the
2026 framework. Requires issuers to send a pre-debit notification at least
24 hours before *any* debit attempt on a recurring mandate — original or
retry.

*Implementation note:* this module doesn't track notification timestamps
itself (no dependency on other tables, per scope). The caller must supply
`last_notification_at`; if it's not supplied, the proposal is vetoed
("cannot confirm ... failing closed") rather than assumed compliant. A
compliance-relevant fact we can't confirm should block, not pass silently.

---

## e-NACH and card e-mandate: configurable defaults

The three NPCI rules above are UPI Autopay-specific — applying them
uniformly to e-NACH/card e-mandate would misrepresent an assumption as
regulation. Both rails instead get:

### Max 3 total attempts

**Operational assumption.** No NPCI/RBI cap on retry count was found for
e-NACH or card e-mandate specifically during research for this module.
Three total attempts (looser than UPI's 4) was chosen as a conservative,
clearly-labeled default — not a citation.

### Minimum 24h spacing between consecutive attempts

**Operational assumption**, and deliberately a different check from UPI's
notification rule: this measures time since the *previous attempt*
(`previous_attempt_at`), not time since a notification event. e-NACH's
physical clearing-cycle nature (see `docs/failure_taxonomy.md` P10) means
attempts are naturally spaced by days anyway, but this rule gives the
constraint store something concrete to enforce even absent a specific
cited figure.

*Note on card e-mandate specifically:* some secondary reporting on RBI's
e-mandate framework describes card e-mandates as covered by the same 24h
pre-debit notification language used for UPI. This project treats that as
an unverified secondary claim (not re-confirmed against RBI's primary
text) and, per the scoping above, applies the same operational-assumption
spacing rule to card e-mandate as to e-NACH rather than borrowing UPI's
cited notification rule. Worth re-verifying against primary RBI text
before treating this as settled.

---

## Universal (all rails)

### Mandate expiry

**Not itself a cited regulation — a lifecycle fact.** Any proposed retry
timestamp after `mandate.mandate_expiry` is vetoed unconditionally. No
debit authorization exists past a mandate's expiry; this isn't a specific
numbered rule so much as what "expired" means.

### Debit-limit awareness: ₹15,000 AFA threshold

**Cited, but informational — never a block.** RBI's Digital Payments –
E-mandate Framework allows recurring transactions up to ₹15,000 to process
without per-attempt additional factor authentication (AFA), once
registered under an e-mandate with AFA at setup. Above ₹15,000, issuers
may require fresh authentication per attempt (originally raised from
₹5,000 to ₹15,000 around mid-2022, consolidated into the 2026 framework).

Mandates above this threshold get a `warnings` entry on an otherwise
allowed `ConstraintResult`, not a veto — this is cost/latency context the
Day-5 planner should factor in (an attempt that might require live AFA is
more expensive/uncertain to execute), not a reason to block the retry
outright.

---

## Summary table

| Rule | Rail(s) | Status | Behavior on violation |
|---|---|---|---|
| Max 4 total attempts | upi_autopay | Cited (NPCI, 2025) | Veto |
| Non-peak windows (13:00–17:00, 21:30–10:00 IST) | upi_autopay | Cited (NPCI, 2025) | Veto (reject, not auto-adjust) |
| ≥24h pre-debit notification | upi_autopay | Cited (RBI e-mandate framework) | Veto (fails closed if unconfirmable) |
| Max 3 total attempts | e_nach, card_emandate | Operational assumption | Veto |
| ≥24h spacing between attempts | e_nach, card_emandate | Operational assumption | Veto (fails closed if unconfirmable) |
| Mandate expiry | all | Lifecycle fact | Veto |
| ₹15,000 AFA threshold | all | Cited (RBI e-mandate framework) | Warning only, never a veto |
