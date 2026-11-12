"""Quality denominators are explicit; absent observations stay unmeasured."""
from collections import Counter, defaultdict
from statistics import mean, median

from agents.outcome import turn_outcome
from evaluation.attribution import wilson
from evaluation.reporting import percentile


def fraction(k, n):
    return {"numerator": k, "denominator": n, "value": k / n if n else None}


def quantiles(values):
    return {f"p{int(q * 100)}": percentile(values, q) for q in (.5, .9, .95, .99)}


def cohen_kappa(cases):
    pairs = [(r["label"]["root_cause"], r["second_label"]["root_cause"]) for r in cases if r["second_label"] is not None]
    n = len(pairs)
    if not n:
        return {"n": 0, "kappa": None}
    first, second = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    observed = sum(a == b for a, b in pairs) / n
    expected = sum(first[k] * second[k] for k in first) / (n * n)
    return {"n": n, "agreement": observed, "kappa": (observed - expected) / (1 - expected) if expected < 1 else None}


def quality_report(rows, feedback, cases, *, verifications=(), approvals=(), task_events=(), unauthorized_writes=0, offline_reports=()):
    n = len(rows)
    signal = lambda name: sum(name in r["signals"] for r in rows)
    write_rows = [r for r in rows if r["quality"].get("write_turn")]
    known_quality = [r for r in rows if r["quality"]]
    inputs = sum(r["input_tokens"] for r in rows)
    tools = [t for r in rows for t in r["tools"]]
    completed_checks = [v for v in verifications if v["status"] in {"passed", "failed", "unknown"}]
    task_ids = {r["task_id"] for r in rows if r["task_id"] is not None}
    reopened = {e["task_id"] for e in task_events if e["kind"] == "progress" and e["text"] == "重新打开事项，等待后续处理。"}
    success_tasks = {v["task_id"] for v in completed_checks if v["status"] == "passed"}
    # Include unsuccessful/retried turns in the numerator, otherwise unit task costs look artificially low.
    task_costs = [r["cost_usd"] for r in rows if r["task_id"] is not None]
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    complete_costs = len(costs) == n and n > 0
    feedback_rids = {f["request_id"] for f in feedback}
    corrected = [r["latency_ms"] for r in rows if "claim_corrected" in r["signals"]]
    ordinary = [r["latency_ms"] for r in rows if "claim_corrected" not in r["signals"]]
    task_rounds = Counter(r["task_id"] for r in rows if r["task_id"] in success_tasks)
    return {
        "n": n,
        "correctness": {
            "goal_verification": fraction(sum(v["status"] == "passed" for v in completed_checks), len(completed_checks)),
            "false_claim": fraction(sum("claim_noted" in r["signals"] for r in write_rows), len(write_rows)),
            "unauthorized_writes": unauthorized_writes,
            "approval_rejected": fraction(sum(a["status"] == "rejected" for a in approvals), len(approvals)),
            "draft_withdrawn": fraction(sum(a["status"] in {"withdrawn", "expired"} and a["confirmed_at"] is None for a in approvals), len(approvals)),
            "task_reopened": fraction(len(reopened & task_ids), len(task_ids)),
        },
        "process": {
            "goal_rounds": quantiles(list(task_rounds.values())),
            "clarification": fraction(sum(r["quality"].get("clarification_needed", False) for r in known_quality), len(known_quality)),
            "clarify_loop": fraction(signal("clarify_loop"), n),
            "tool_errors": {key: fraction(sum(t["error_class"] == key for t in tools), len(tools)) for key in sorted({t["error_class"] for t in tools})},
            "model_requests_mean": mean(r["model_requests"] for r in rows) if n else None,
            "round_limit": fraction(signal("round_limit"), n),
            "intent_sources": dict(Counter(r["intent_source"] for r in rows)),
            "intent_rule_fallback": fraction(signal("intent_rule_fallback"), n),
            "intent_llm_fallback": fraction(sum(r["intent_source"].startswith("llm") for r in rows), n),
            "retrieval_evidence": fraction(sum(r["quality"].get("knowledge_used", False) for r in known_quality), len(known_quality)),
            "escalated": fraction(signal("escalated"), n),
            "claim_corrected": fraction(signal("claim_corrected"), n),
            "claim_correction_latency": {"overhead_ms": quantiles([r["stage_ms"]["claim_correction"] for r in rows if "claim_correction" in r["stage_ms"]]), "corrected": quantiles(corrected), "ordinary": quantiles(ordinary),
                "median_difference_ms": median(corrected) - median(ordinary) if corrected and ordinary else None,
                "interpretation": "observational group difference; not causal correction overhead"},
        },
        "experience": {
            "first_token_ms": quantiles([r["first_token_ms"] for r in rows if r["first_token_ms"] is not None]),
            "latency_ms": quantiles([r["latency_ms"] for r in rows]),
            "answer_reset": fraction(signal("answer_reset"), n),
            "negative_feedback": fraction(sum(f["rating"] == -1 for f in feedback), len(feedback)),
            "feedback_coverage": fraction(len(feedback_rids), n), "rephrase": fraction(signal("rephrase"), n),
        },
        "cost": {"per_turn_usd": sum(costs) / n if complete_costs else None, "priced_turns": len(costs),
            "per_successful_task_usd": sum(task_costs) / len(success_tasks) if success_tasks and task_costs and all(c is not None for c in task_costs) else None,
            "cache_read_ratio": sum(r["cache_read_tokens"] for r in rows) / inputs if inputs else None,
            "tokens_p95": percentile([r["input_tokens"] + r["output_tokens"] for r in rows], .95)},
        "stability": {"versions": {version: {"n": len(group), "success_rate": sum(turn_outcome(r) == "success" for r in group) / len(group),
            "wilson_95": wilson(sum(turn_outcome(r) == "success" for r in group), len(group))}
            for version, group in _by_version(rows).items()}, "version_differences": _version_differences(rows), "offline_pass_k": list(offline_reports)},
        "label_quality": cohen_kappa(cases),
    }


def _by_version(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["agent_version"]].append(row)
    return grouped


def _version_differences(rows):
    groups = _by_version(rows)
    versions = sorted(groups)
    result = []
    for index, left in enumerate(versions):
        for right in versions[index + 1:]:
            a, b = groups[left], groups[right]
            p0, p1 = sum(turn_outcome(r) == "success" for r in a) / len(a), sum(turn_outcome(r) == "success" for r in b) / len(b)
            i0 = wilson(sum(turn_outcome(r) == "success" for r in a), len(a))
            i1 = wilson(sum(turn_outcome(r) == "success" for r in b), len(b))
            # Newcombe's independent-samples interval combines the Wilson distances.
            lower = p1 - p0 - ((p1 - i1[0]) ** 2 + (i0[1] - p0) ** 2) ** .5
            upper = p1 - p0 + ((i1[1] - p1) ** 2 + (p0 - i0[0]) ** 2) ** .5
            result.append({"baseline_version": left, "current_version": right, "baseline_n": len(a), "current_n": len(b),
                           "difference": p1 - p0, "wilson_difference_95": [max(-1., lower), min(1., upper)]})
    return result
