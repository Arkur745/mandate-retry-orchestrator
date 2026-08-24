# Demo-ready mandates

Curated by `scripts/seed_demo_mandates.py`, run against a fresh `orchestrator.db` alongside a 15-mandate/30-event general background batch (so the trace viewer's search/filter has something realistic to search through, not just these five). All LLM content below is real Groq output, not mocked — see the note on model choice at the bottom.

Open the trace viewer at **http://127.0.0.1:8000/**, search by mandate ID in the sidebar, and click to load the timeline.

| # | Mandate ID | Scenario | What to point out on camera |
|---|---|---|---|
| 1 | **#16** | Clean rule-path success | Failure → rule classification (zero LLM calls, instant, cited taxonomy lookup) → 3-step front-loaded retry plan → all 3 attempts execute successfully. The "boring, good" case — most of the pipeline's volume looks like this. |
| 2 | **#17** | LLM-path classification (P3, post ground-truth fix) | The explicit-"Suspected Fraud" P3 variant. Classification card is LLM-badged with real reasoning inline: *"Fraud suspicion flag means retries will likely be declined until user resolves with bank."* Escalates (`not_recoverable`), then a real LLM-generated `merchant_escalation` message. This is the strongest single frame for "AI Judgment" — the reasoning text is visible, not buried in JSON. |
| 3 | **#18** | P9 / `rail_switch_recommended` | Card e-mandate, issuing-bank-downtime failure. Rule-classified recoverable, but the plan escalates (`all_candidates_vetoed`) — the structural rail-spacing collision from Day 5/6/9, not a bug. Real LLM fallback content recommends switching the customer to UPI Autopay. Good frame for explaining a deliberately-not-fixed finding. |
| 4 | **#19** | Dead-zone fix, post-fix timing | Same category as #16 (P2/upi_autopay), but `occurred_at` is set to **17:05 IST — the exact timestamp** that produced zero valid candidates before the Day 9 fix (`docs/eval_audit.md`). Now plans and executes successfully. There's no "before" mandate to show side by side (the fix is in the code, not a toggle — reverting it to construct a live "broken" comparison would mean shipping broken code, which wasn't the ask) — the story is told by citing the exact timestamp and pointing at `docs/eval_audit.md`'s Day 9 entry, which documents the pre-fix behavior at this same timestamp. |
| 5 | **#20** | `retry_exhausted_nudge` (forced) | Day 10's multi-seed eval found this escalation type doesn't occur naturally at small batch sizes. Forced deliberately here via a tiny mandate amount (INR 1.00), driving `EV_ESCALATE_THRESHOLD_PAISE`'s negative-EV branch — same technique as the Day 9 S7 test. Real LLM fallback content: *"We couldn't automatically complete your payment of Rs 1.00..."* |

## Note on model choice for this seed run

`GROQ_MODEL` in `.env` (and everywhere else in the project) is `openai/gpt-oss-20b`, the model tested and documented throughout. Groq's free-tier daily token quota (200,000 TPD) was exhausted by the cumulative volume of real-API testing done earlier the same day (Day 11's clean-clone verification, the general background batch, etc.) — confirmed via the actual `429 rate_limit_exceeded` errors captured in `audit_log`, not assumed. Rather than ship demo mandates whose "LLM" cards silently show S1/S8 safe-default fallback text instead of genuine model reasoning, this specific seed run overrode `GROQ_MODEL=openai/gpt-oss-120b` (same model family, confirmed to have separate remaining quota) for the duration of the script only, via an environment variable, not a change to `.env` or any pipeline code. All five scenarios above reflect real model output. If you re-seed and hit the same quota wall, the same override (`GROQ_MODEL=openai/gpt-oss-120b python scripts/seed_demo_mandates.py`) is the fastest way around it.

## Re-seeding

```
rm -f orchestrator.db
python scripts/seed_demo_mandates.py
```

Mandate IDs are assigned in creation order, so a fresh run reproduces the same IDs (16–20) as long as the 15-mandate background batch runs first, unchanged, as it does in the script.
