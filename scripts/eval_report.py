"""Render the eval harness (app/eval.py) as a single markdown report.

    venv/Scripts/python.exe scripts/eval_report.py > eval_report.md
    venv/Scripts/python.exe scripts/eval_report.py --skip-s-checklist   # faster, skips re-running tests

This is the artifact to look at during Day 9 stress-testing and to quote
numbers from in the demo video -- regenerate it any time the DB changes.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal
from app.eval import (
    classifier_accuracy_report,
    escalation_type_distribution,
    run_s_checklist,
    simulated_recovered_revenue,
)


def render(db, *, run_s_checklist_flag: bool) -> str:
    lines: list[str] = []
    lines.append("# Eval report")
    lines.append("")
    lines.append(f"Generated: {datetime.now(timezone.utc).isoformat()}Z")
    lines.append("")

    # --- Classifier accuracy ---
    lines.append("## Classifier accuracy vs. ground_truth_recoverable")
    lines.append("")
    acc = classifier_accuracy_report(db)
    lines.append("| Category | Scored | Correct | Accuracy | Unscored (ambiguous/null) |")
    lines.append("|---|---|---|---|---|")
    for taxonomy_id, cat in acc.per_category.items():
        acc_str = f"{cat.accuracy:.1%}" if cat.accuracy is not None else "n/a"
        lines.append(f"| {taxonomy_id} | {cat.scored} | {cat.correct} | {acc_str} | {cat.unscored_ambiguous} |")
    agg_str = f"{acc.aggregate_accuracy:.1%}" if acc.aggregate_accuracy is not None else "n/a"
    lines.append(f"| **Aggregate** | **{acc.total_scored}** | **{acc.total_correct}** | **{agg_str}** | |")
    lines.append("")

    # --- Escalation-type distribution ---
    lines.append("## Escalation-type distribution")
    lines.append("")
    dist = escalation_type_distribution(db)
    total = sum(dist.values())
    lines.append("| Escalation type | Count | Fraction |")
    lines.append("|---|---|---|")
    for etype, count in dist.items():
        frac = f"{count / total:.1%}" if total else "n/a"
        lines.append(f"| {etype} | {count} | {frac} |")
    lines.append(f"| **Total** | **{total}** | |")
    lines.append("")

    # --- Simulated recovered revenue ---
    lines.append("## Simulated recovered revenue")
    lines.append("")
    lines.append(
        "**This is an expected value under the planner's own hand-specified probability "
        "model (app/planner.py), not an empirical or real-world recovery-rate claim.** No "
        "real debits were attempted against synthetic mandates. Per plan: "
        "`P(recovered) = 1 - product(1 - p_i)` over the plan's steps (the standard \"at least "
        "one attempt succeeds\" treatment) times `mandate.amount`. This is deliberately NOT "
        "the same formula as the planner's own search-time expected value (which sums "
        "`p_i * amount - cost_i` additively as a search heuristic, not a probability-correct "
        "recovery estimate) -- reusing that formula here would double-count revenue across "
        "multi-step plans."
    )
    lines.append("")
    revenue = simulated_recovered_revenue(db)
    lines.append(f"- Retry plans included: {revenue.retry_plan_count}")
    lines.append(f"- Simulated recovered revenue (model-internal estimate): "
                  f"INR {revenue.total_inr:,.2f} ({revenue.total_paise:,.0f} paise)")
    lines.append("")
    lines.append("| Category | Estimated recovered (INR) |")
    lines.append("|---|---|")
    for taxonomy_id in sorted(revenue.per_category, key=lambda c: int(c[1:])):
        lines.append(f"| {taxonomy_id} | {revenue.per_category[taxonomy_id] / 100:,.2f} |")
    lines.append("")

    # --- S1-S9 checklist ---
    lines.append("## S1-S9 system-failure-mode checklist")
    lines.append("")
    lines.append(
        "Status is determined by actually re-running the mapped test(s) via pytest, not by "
        "a cached claim. \"no_tests\" means the failure mode's mitigation code exists but no "
        "test in the suite drives execution to that exact branch."
    )
    lines.append("")
    if run_s_checklist_flag:
        results = run_s_checklist()
        lines.append("| Code | Description | Status | Tests |")
        lines.append("|---|---|---|---|")
        for r in results:
            status_label = {"passing": "PASS", "no_tests": "**GAP (no test)**", "failing": "**FAIL**"}[r.status]
            test_list = "<br>".join(f"`{t}`" for t in r.test_node_ids) or "-"
            lines.append(f"| {r.code} | {r.description} | {status_label} | {test_list} |")
        lines.append("")
        for r in results:
            if r.note:
                lines.append(f"**{r.code} note:** {r.note}")
                lines.append("")
    else:
        lines.append("(skipped -- run without --skip-s-checklist to actually re-run the mapped tests)")
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-s-checklist", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        report = render(db, run_s_checklist_flag=not args.skip_s_checklist)
    finally:
        db.close()

    print(report)


if __name__ == "__main__":
    main()
