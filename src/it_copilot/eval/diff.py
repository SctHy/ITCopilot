"""Compare median observations by experiment, arm and metric."""

from decimal import Decimal
import math

LOWER = set(
    """
asr fpr clean_zh_fpr clean_replaced_chars unauthorized_write_rate cross_tenant_leak
pii_leak_rate leaking_query_rate duplicate_external_write_rate duplicate_writes_per_case
idle_empty_claims_per_s empty_claims_per_s failed_primary_calls deadline_breaker_changes
cross_tenant_hits cross_department_hits cross_tenant_leaks duplicate_writes duplicate_write_rate
unauthorized_writes empty_query_rate estimated_cost_usd p50 p95 p99 noncompliant_calls input_tokens peak_turn_tokens summary_input_tokens cost_usd observe_ms_per_case
""".split()
)
HIGHER = set(
    """
consistency final_consistency drift_detection_rate b_window_completion jain a_window_per_s
precision f1 coverage tool_restore_rate state_detection anchor_retention prefix_hit_rate
availability speedup
""".split()
)


def direction(metric):
    key = metric.rsplit("/", 1)[-1].lower()
    if key in LOWER or key.endswith("_ms"):
        return -1
    if key in HIGHER or key.startswith(("recall@", "ndcg@", "mrr@")):
        return 1
    raise ValueError(f"unknown metric direction: {metric}")


def by_name(rows):
    indexed = {row["name"]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError("duplicate name in ledger")
    return indexed


def compare_ledgers(baseline, candidate, threshold=0.15, allow_missing=False):
    if not math.isfinite(threshold) or threshold < 0:
        raise ValueError("threshold must be finite and nonnegative")
    tolerance = Decimal(str(threshold))
    rows = []

    def record(exp, arm, metric, status, before=None, after=None, change=None):
        rows.append(
            dict(
                experiment=exp,
                arm=arm,
                metric=metric,
                status=status,
                baseline=before,
                candidate=after,
                relative_change=change,
            )
        )

    candidates = by_name(candidate["experiments"])
    for name, exp in candidates.items():
        if not exp["passed"]:
            record(name, "", "", "INVARIANT_FAIL")
    for name, exp in by_name(baseline["experiments"]).items():
        if name not in candidates:
            record(name, "", "", "MISSING")
            continue
        arms = by_name(candidates[name]["arms"])
        for label, arm in by_name(exp["arms"]).items():
            if label not in arms:
                record(name, label, "", "MISSING")
                continue
            for metric, observation in arm["metrics"].items():
                other = arms[label]["metrics"].get(metric)
                if other is None:
                    record(name, label, metric, "MISSING_METRIC")
                    continue
                before, after = observation["median"], other["median"]
                if not all(map(math.isfinite, (before, after))):
                    raise ValueError(f"nonfinite median for {name}/{label}/{metric}")
                reference = Decimal(str(before))
                gain = (Decimal(str(after)) - reference) * direction(metric)
                limit = abs(reference) * tolerance
                status = "NEUTRAL"
                if abs(gain) > limit:
                    status = "IMPROVED" if gain > 0 else "REGRESSED"
                record(
                    name,
                    label,
                    metric,
                    status,
                    before,
                    after,
                    (after - before) / abs(before) if before else None,
                )
    failing = {"REGRESSED", "INVARIANT_FAIL", "MISSING_METRIC"}
    if not allow_missing:
        failing.add("MISSING")
    return dict(
        threshold=threshold,
        allow_missing=allow_missing,
        rows=rows,
        passed=all(row["status"] not in failing for row in rows),
    )


def terminal(result):
    lines = ["Gate            Experiment / Arm / Metric    Baseline → Candidate"]
    for row in result["rows"]:
        path = " / ".join(filter(None, (row["experiment"], row["arm"], row["metric"])))
        values = (
            "" if row["baseline"] is None else f"{row['baseline']:.5g} → {row['candidate']:.5g}"
        )
        lines.append(f"{row['status']:<15} {path}    {values}")
    return "\n".join([*lines, "PASS" if result["passed"] else "FAIL"])
