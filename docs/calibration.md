# Calibration mechanism

The retry planner's probability model (`app/planner.py`'s `base_probability`
and `decay_per_step`, per `TimingProfile`) is hand-specified, not fit from
data — the module's own docstring says so, and each profile carries a
one-line rationale comment instead of a citation to a dataset. That's a
reasonable starting point, but it's also exactly the kind of claim that
should eventually be checked against outcomes. This mechanism is that check
— three separate, human-gated steps, not a live feedback loop.

## The three steps

1. **`scripts/calibration_report.py`** (read-only) — for each category with
   enough executed `retry_attempts` to be meaningful, computes the
   empirical per-step success rate and compares it to the planner's
   current hand-specified prior. Prints a table: category, step, predicted
   probability, empirical probability, sample size, delta. Also breaks
   down by rail for categories that span more than one (e.g. P2 exists on
   all three rails). Never writes anything, anywhere.

2. **`scripts/propose_priors.py`** (still read-only w.r.t. the live system)
   — runs the same calibration, then for every category that both clears
   the sample-size threshold and shows a delta worth acting on, fits a new
   `base_p`/`decay` from the empirical data and writes them to
   `app/priors_proposed.json`. This file is a draft. **Nothing in this
   codebase reads it automatically** — not the planner, not any other
   script. It exists to be reviewed by a human, printed as a clear diff
   against the current values.

3. **`scripts/adopt_priors.py`** (the one write path) — copies specific,
   *named* categories from `priors_proposed.json` into
   `app/priors_active.json`, the one file `app.planner._effective_profile`
   actually reads to override `CATEGORY_PROFILES`' hand-specified numbers.
   Requires `--categories` explicitly; there is no "adopt everything"
   default. Writes one `audit_log` row (`event_type="priors_adopted"`)
   recording, per category, the before value, the after value, and a note
   that this was a deliberate action. The planner picks up the change on
   its very next `plan_retries` call — no restart, no redeploy, no other
   file to touch — because `_effective_profile` re-reads
   `priors_active.json` on every call rather than caching it at import.

Two categories can share one `TimingProfile` object today (P2 and P9 both
use `_FAST_TECHNICAL`; P3 and P12 both use `_CAUTIOUS_SINGLE`). Calibration
and adoption both operate strictly per `taxonomy_id`: `_effective_profile`
returns a *copy* of the shared profile with only the named category's
numbers replaced, so adopting a new value for P2 can never silently change
what P9 does. See `tests/test_calibration.py`'s
`test_effective_profile_picks_up_adopted_value_without_touching_profile_sibling`.

## Thresholds, and why these specific numbers

- **`MIN_SAMPLE_SIZE = 20`** (`app/calibration.py`) — the minimum number of
  *executed* attempts (`outcome` in `success`/`failed`; `pending`,
  `executing`, `skipped` carry no signal yet) at a given (category, step)
  before an empirical rate is reported at all. For a true rate of 0.5, the
  standard error of a sample proportion at n=20 is about 11 percentage
  points — already coarse, but at n=2 (the explicit non-example in the
  brief for this task) a single flipped outcome changes the "rate" by 50
  points. 20 is a deliberately round, conservative floor: enough that a
  reported delta reflects something, not a demand for real statistical
  power on a demo-scale dataset.

- **`DELTA_THRESHOLD = 0.10`** (`app/calibration.py`) — a predicted-vs-
  empirical gap smaller than 10 percentage points is squarely inside the
  range a reasonable hand guess could land in. The priors were explicitly
  scoped as estimates, not measurements; treating every small wobble as
  "the model is wrong" would mean re-fitting on noise every time the
  batch changes. Only a gap big enough that no reasonable hand-specified
  guess would plausibly land inside it gets flagged as `DRIFT` in the
  report, or proposed as a change.

Both are constants at the top of `app/calibration.py`, not buried —
change them there if the project's risk tolerance changes, and the
docstring on each explains the tradeoff being made.

## Why this is not a live, automatic feedback loop

Two separate reasons, and both are load-bearing on their own:

**Circularity.** Every number this mechanism reads comes from
`app/simulator.py`'s synthetic failure injector and `app/executor.py`'s
stub/test-mode debit call — this system's own simulation of itself, not
real Razorpay outcomes. If the planner's priors fed the simulator (or the
simulator's outcome model matched the priors by construction) and the
calibration loop fed back into the planner automatically, the system would
converge to agreeing with itself and call it validation. That's not a
hypothetical risk to guard against later — it's the literal shape of the
data available in this project today. The three-step design makes that
explicit rather than hiding it: the calibration report's own output header
states unmissably that it validates the *mechanism*, not the *priors*
against reality.

**Compliance and safety, in a payments context.** A model change that
shifts how aggressively a payment mandate gets retried is not a cosmetic
tuning parameter — it affects how often a customer's bank sees repeated
debit attempts (with real bank-side fraud-flagging and NPCI rate-limit
consequences, per this project's own P3/P6/P7/P11 findings), how much
retry cost is spent, and how quickly a merchant sees revenue recognized or
an escalation fires instead. Auto-adjusting that from a live feedback loop
— even a well-intentioned one — removes the one checkpoint where a human
looks at *why* a number moved before it moves. The audit_log entry
`adopt_priors.py` writes exists specifically so "why is this category
retrying differently now" always has a one-hop answer: a named person ran
a named script naming a named category, and here's the before and after.

## What would need to be true for this to run unattended

Honestly: real Razorpay-side outcome data, not simulated data, is the one
prerequisite that actually matters. Specifically:

- `retry_attempts.outcome` would need to be populated by real Razorpay API
  responses for mandates with a completed `razorpay_token` (the
  `app.executor._real_debit_call` path already exists and is structurally
  correct — it is, as documented in `docs/architecture.md`, unexercised
  against a real authorized mandate as of this build, since no real
  registration link was ever clicked through). Calibration against stub
  outcomes, no matter how large the batch, can only ever validate the
  *mechanism*.
- Even with real outcomes, "unattended" would still require a second
  thing this project doesn't have: a monitoring/alerting layer that can
  tell the difference between "the empirical rate moved because real
  customer behavior shifted" and "the empirical rate moved because a
  Razorpay-side outage or a bug is silently failing every attempt" — an
  automatic feedback loop that can't tell those apart will confidently
  learn the wrong thing from an incident. That's a genuinely different
  (and larger) piece of infrastructure than anything built for this
  project.

Until both of those are true, three deliberate steps with a human at the
adoption boundary is the right amount of automation, not a shortcut this
project didn't have time for.
