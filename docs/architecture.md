# Architecture explanation

Draft written incrementally during the build. Final version submitted alongside the demo video.

TODO: fill in as each pipeline stage is built (see docs/build_schedule.md).

## Retry planner (app/planner.py): EV tie-break rule

When two or more candidate sequences have identical expected value — which
happens whenever a timing profile offers multiple offsets in the same slot
(e.g. `fast_technical`'s `(0.5, 1.0)` first slot), since probability/cost
in this model depend only on step *position*, not which offset was chosen
within a slot — the planner breaks the tie in this priority order:

1. **Fewer attempts.** A shorter sequence hitting the same modeled EV
   carries less real-world bank-flagging/rate-limit exposure than the EV
   formula captures, so it's the safer choice when the model can't
   distinguish two options on paper.
2. **Earliest schedule**, among sequences still tied after (1). Resolving
   sooner shortens the customer's at-risk window and the merchant's
   revenue-recognition delay, at zero EV cost.

Implemented as a sort key `(-expected_value, len(offsets), offsets)` —
see `app/planner.py`'s `plan_retries` for the code comment.
