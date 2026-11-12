"""Metadata-only success attribution. Contributions use failure-rate percentage points."""
from __future__ import annotations

from collections import Counter
from math import erfc, sqrt
from statistics import median

from agents.outcome import turn_outcome

DIMENSIONS = (
    "agent_version", "model", "route_reason", "intent_group", "intent_source", "channel",
    "tenant", "policy_version", "kb_version", "skill_fp", "prompt_fp", "tools_fp",
    "model_requests_bucket", "input_tokens_bucket",
)


def wilson(successes: int, total: int) -> list[float] | None:
    if total == 0:
        return None
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [max(0., centre - half), min(1., centre + half)]


def summary(rows: list[dict]) -> dict:
    n = len(rows)
    successes = sum(turn_outcome(row) == "success" for row in rows)
    return {"n": n, "successes": successes, "success_rate": successes / n if n else None,
            "wilson_95": wilson(successes, n)}


def bucket(row: dict, dimension: str):
    if dimension == "model_requests_bucket":
        n = row["model_requests"]
        return "0" if n == 0 else "1" if n == 1 else "2-3" if n <= 3 else "4+"
    if dimension == "input_tokens_bucket":
        n = row["input_tokens"]
        return "<1k" if n < 1000 else "1k-4k" if n < 4000 else "4k-16k" if n < 16000 else "16k+"
    return row[dimension]


def token_summary(rows: list[dict]) -> dict:
    inputs = sum(row["input_tokens"] for row in rows)
    return {
        "model_requests_median": median(row["model_requests"] for row in rows) if rows else None,
        "input_tokens_median": median(row["input_tokens"] for row in rows) if rows else None,
        "cache_read_ratio": sum(row["cache_read_tokens"] for row in rows) / inputs if inputs else None,
    }


def slices(before: list[dict], after: list[dict], dimension: str) -> list[dict]:
    values = {bucket(row, dimension) for row in before + after}
    result = []
    for value in sorted(values, key=str):
        b = [row for row in before if bucket(row, dimension) == value]
        a = [row for row in after if bucket(row, dimension) == value]
        s0, s1 = len(b) / len(before) if before else 0, len(a) / len(after) if after else 0
        f0 = sum(turn_outcome(row) != "success" for row in b) / len(b) if b else None
        f1 = sum(turn_outcome(row) != "success" for row in a) / len(a) if a else None
        # A newly observed slice has no measured baseline rate; assign its contribution to mix.
        base = f0 if f0 is not None else f1
        current = f1 if f1 is not None else base
        result.append({"dimension": dimension, "value": value, "baseline_n": len(b), "current_n": len(a),
                       "baseline_failure_rate": f0, "current_failure_rate": f1,
                       "rate": s1 * (current - base), "mix": base * (s1 - s0),
                       "baseline_tokens": token_summary(b), "current_tokens": token_summary(a),
                       "comparable": bool(b and a)})
    return sorted(result, key=lambda item: abs(item["rate"]), reverse=True)


def proportion_test(before: list[dict], after: list[dict]) -> dict:
    n0, n1 = len(before), len(after)
    if not n0 or not n1:
        return {"baseline_n": n0, "current_n": n1, "p_value": None, "significant": False}
    k0 = sum(turn_outcome(row) != "success" for row in before)
    k1 = sum(turn_outcome(row) != "success" for row in after)
    pooled = (k0 + k1) / (n0 + n1)
    se = sqrt(pooled * (1 - pooled) * (1 / n0 + 1 / n1))
    z = (k1 / n1 - k0 / n0) / se if se else 0.
    p_value = erfc(abs(z) / sqrt(2))
    return {"baseline_n": n0, "current_n": n1, "baseline_failure_rate": k0 / n0,
            "current_failure_rate": k1 / n1, "p_value": p_value,
            "significant": p_value < .05, "failure_increased": z > 0}


def tool_rates(rows: list[dict]) -> dict:
    totals, counts = Counter(), Counter()
    for row in rows:
        for tool in row["tools"]:
            totals[tool["name"]] += 1
            counts[(tool["name"], tool["error_class"])] += 1
    return {key: {"calls": n, "total": totals[key[0]], "rate": n / totals[key[0]]}
            for key, n in counts.items()}


def attribute(before: list[dict], after: list[dict], events: list[dict], *, snapshots=None) -> dict:
    decomposition = {dim: slices(before, after, dim) for dim in DIMENSIONS}
    top = sorted((row for group in decomposition.values() for row in group),
                 key=lambda item: (abs(item["rate"]), abs(
                     (item["current_failure_rate"] or 0) - (item["baseline_failure_rate"] or 0))),
                 reverse=True)[:10]
    by_dimension = {dim: {"rate": sum(row["rate"] for row in group),
                          "mix": sum(row["mix"] for row in group)}
                    for dim, group in decomposition.items()}
    btools, atools = tool_rates(before), tool_rates(after)
    btotals = Counter(tool["name"] for row in before for tool in row["tools"])
    atotals = Counter(tool["name"] for row in after for tool in row["tools"])
    tools = []
    for key in btools.keys() | atools.keys():
        if key[1] == "success":
            continue
        b = btools.get(key, {"calls": 0, "total": btotals[key[0]], "rate": 0.})
        a = atools.get(key, {"calls": 0, "total": atotals[key[0]], "rate": 0.})
        tools.append({"tool": key[0], "error_class": key[1], "baseline": b, "current": a,
                      "delta": a["rate"] - b["rate"]})
    timeline = []
    for event in events:
        at = event["at"]
        b = [row for row in before + after if at - 7200 <= row["created_at"] < at]
        a = [row for row in before + after if at <= row["created_at"] < at + 7200]
        timeline.append({"event": event, **proportion_test(b, a)})
    hours = sorted({int(row["created_at"] // 3600) * 3600 for row in before + after})
    hourly = [{"at": hour, **summary([row for row in before + after
                                     if hour <= row["created_at"] < hour + 3600])} for hour in hours]
    samples = []
    snapshots = {} if snapshots is None else snapshots
    for item in top[:3]:
        failed = [row for row in after if bucket(row, item["dimension"]) == item["value"]
                  and turn_outcome(row) != "success"][:5]
        samples.append({"dimension": item["dimension"], "value": item["value"], "requests": [
            {"request_id": row["request_id"], "trace": "/trace/" + row["request_id"],
             "badcase_id": snapshots.get(row["request_id"])} for row in failed]})
    dominant = max(by_dimension, key=lambda dim: (
        round(abs(by_dimension[dim]["mix"]) + abs(by_dimension[dim]["rate"]), 12),
        len(decomposition[dim])))
    contribution = by_dimension[dominant]
    return {"baseline": summary(before), "current": summary(after), "top_slices": top,
            "dimensions": decomposition, "contributions": by_dimension,
            "dominant_dimension": dominant,
            "diagnosis": ("mix" if abs(contribution["mix"]) > abs(contribution["rate"]) else "rate") if before and after else "insufficient_data",
            "tool_errors": sorted(tools, key=lambda row: abs(row["delta"]), reverse=True)[:10],
            "tokens": {"baseline": token_summary(before), "current": token_summary(after)},
            "timeline": timeline, "hourly": hourly, "samples": samples,
            "limitations": ["Change alignment is correlation, not causation.",
                            "Per-dimension decompositions overlap and must not be added together.",
                            "Event tests are exploratory, without multiple-comparison correction.",
                            "New slices lack a measured baseline and are assigned to mix."]}


def markdown(report: dict) -> str:
    def percent(value):
        return "无样本" if value is None else f"{value:.2%}"
    lines = ["# 成功率波动归因", "", "| 窗口 | 样本数 | 成功率 | Wilson 95% 区间 |",
             "|---|---:|---:|---|"]
    for name in ("baseline", "current"):
        row = report[name]
        interval = row["wilson_95"]
        lines.append(f"| {name} | {row['n']} | {percent(row['success_rate'])} | "
                     + (" / ".join(map(percent, interval)) if interval else "无样本") + " |")
    lines += ["", f"主要贡献类型：{report['diagnosis']}；维度：{report['dominant_dimension']}。",
              "", "| 维度 | 切片 | n0 / n1 | rate | mix |", "|---|---|---:|---:|---:|"]
    for row in report["top_slices"]:
        lines.append(f"| {row['dimension']} | {row['value']} | {row['baseline_n']} / {row['current_n']} | "
                     f"{percent(row['rate'])} | {percent(row['mix'])} |")
    lines += ["", "## 变更时间线", ""]
    for row in report["timeline"]:
        event = row["event"]
        lines.append(f"- {event['at']} {event['kind']} {event['ref']}: p={row['p_value']}, "
                     f"significant={row['significant']} ({row['baseline_n']}/{row['current_n']})")
    lines += ["", "## 工具错误变化", ""]
    for row in report["tool_errors"]:
        lines.append(f"- {row['tool']} / {row['error_class']}: {percent(row['baseline']['rate'])} → "
                     f"{percent(row['current']['rate'])}; calls={row['baseline']['total']}/{row['current']['total']}")
    lines += ["", "## Token", "", str(report["tokens"]), "", "## 失败样本", ""]
    for group in report["samples"]:
        lines.append(f"- {group['dimension']}={group['value']}")
        lines.extend(f"  - [{row['request_id']}]({row['trace']}), badcase={row['badcase_id']}"
                     for row in group["requests"])
    lines += ["", "这些统计用于定位候选原因；变更与失败的时间相关性需要读样本确认。"]
    return "\n".join(lines)
