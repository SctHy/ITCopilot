"""Summarize matched live application trials separately from fixed-delay streaming benchmarks."""
import argparse
import hashlib
from collections import Counter
from datetime import datetime, timezone
import json
from math import sqrt
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.attribution import wilson  # noqa: E402
from evaluation.reporting import percentile  # noqa: E402


def quantiles(values):
    return {"n": len(values), **{f"p{int(q*100)}": percentile(values, q) for q in (.5, .95, .99)}}


def summarize(applications, streams):
    trials = [trial for report in applications for trial in report["trials"]]
    turns = [turn for trial in trials for turn in trial["turns"]]
    stages = [turn["response"]["stage_ms"] for turn in turns]
    passed = sum(trial["passed"] for trial in trials)
    agent_cost = sum(trial["cost_usd"] for trial in trials)
    sdk_cost = sum(report["token_audit"]["cost_usd_peak"] for report in applications)
    sdk_usage = {key: sum(report["token_audit"]["usage"][key] for report in applications)
                 for key in ("prompt", "hit", "miss", "output", "reasoning")}
    routes = {}
    intent_sources = Counter()
    for turn in turns:
        route = turn["trace"]["trace"]["observability"]["versions"]["route"] if turn["trace"].get("found", False) else "unrecorded"
        intent_sources[turn["trace"]["trace"]["observability"]["intent"]["source"] if turn["trace"].get("found", False) else "unrecorded"] += 1
        counts = routes.setdefault(route, {"turns": 0, "round_limit": 0, "deadline": 0})
        counts["turns"] += 1
        counts["round_limit"] += turn["response"]["error_code"] == "round_limit"
        counts["deadline"] += turn["response"]["error_code"] == "deadline"
    return {
        "live": {"cases": len(trials), "passed": passed, "success_rate": passed / len(trials),
            "success_wilson_95": wilson(passed, len(trials)), "completed_turns": len(turns),
            "case_latency_s": quantiles([trial["latency_s"] for trial in trials]),
            "agent_latency_ms": quantiles([turn["response"]["latency_ms"] for turn in turns]),
            "pipeline_stage_total_ms": quantiles([sum(stage.values()) for stage in stages]),
            "stage_p99_ms": {name: percentile([stage[name] for stage in stages if name in stage], .99)
                             for name in sorted({name for stage in stages for name in stage})},
            "agent_cost_usd": agent_cost, "agent_cost_per_success_usd": agent_cost / passed if passed else None,
            "sdk_returned_usage_cost_usd_peak": sdk_cost,
            "sdk_returned_usage_cost_per_success_usd_peak": sdk_cost / passed if passed else None,
            "potentially_unreported_model_usage_turns": sum(turn["response"]["error_code"] in {"deadline", "model_unavailable"} for turn in turns),
            "sdk_usage": sdk_usage, "sdk_cache_hit_rate": sdk_usage["hit"] / sdk_usage["prompt"] if sdk_usage["prompt"] else None,
            "sdk_calls": sum(report["token_audit"]["calls"] for report in applications), "routes": routes,
            "intent_sources": dict(intent_sources)},
        "fixed_delay_stream": {str(report["concurrency"]): {"runs": report["runs"], "repeats": report["repeats"],
            "summary": report["summary"], "stage_p99_ms": report["stage_p99_ms"]} for report in streams},
        "datasets": [{"dataset": report["dataset"], "sha256": report["dataset_sha256"],
                      "code_sha256": report["code_sha256"], "summary": report["summary"],
                      "case_repeats": sorted([[trial["case_id"], trial["repeat"]] for trial in report["trials"]])} for report in applications],
    }


def compare(baseline, after):
    signature = lambda report: sorted((row["dataset"], row["sha256"], row["summary"]["trials"], row["case_repeats"]) for row in report["datasets"])
    if signature(baseline) != signature(after):
        raise ValueError("Matched datasets and repetition counts are required")
    if baseline["fixed_delay_stream"].keys() != after["fixed_delay_stream"].keys():
        raise ValueError("Matched stream concurrencies are required")
    before, current = baseline["live"], after["live"]
    if before["sdk_usage"]["prompt"] == 0 or current["sdk_usage"]["prompt"] == 0:
        raise ValueError("Both windows require measured model token usage; verify credentials and provider health")
    delta = current["success_rate"] - before["success_rate"]
    b_low, b_high = before["success_wilson_95"]
    a_low, a_high = current["success_wilson_95"]
    lower = delta - sqrt((current["success_rate"] - a_low) ** 2 + (b_high - before["success_rate"]) ** 2)
    upper = delta + sqrt((a_high - current["success_rate"]) ** 2 + (before["success_rate"] - b_low) ** 2)
    reductions = {n: 1 - after["fixed_delay_stream"][n]["summary"]["completed_s"]["p99"] /
        values["summary"]["completed_s"]["p99"] for n, values in baseline["fixed_delay_stream"].items()}
    before_cost, after_cost = before["sdk_returned_usage_cost_per_success_usd_peak"], current["sdk_returned_usage_cost_per_success_usd_peak"]
    def reduction(name):
        initial, final = before[name]["p99"], current[name]["p99"]
        return 1 - final / initial if initial is not None and final is not None and initial > 0 else None
    agent_reduction = reduction("agent_latency_ms")
    pipeline_reduction = reduction("pipeline_stage_total_ms")
    return {"success_rate_delta": delta, "success_delta_newcombe_wilson_95": [lower, upper],
        "success_noninferior": delta >= -.02 or upper >= 0,
        "sdk_returned_usage_cost_per_success_nonincreasing": after_cost <= before_cost if after_cost is not None and before_cost is not None else None,
        "cost_target_nonincreasing": after_cost <= before_cost if before["potentially_unreported_model_usage_turns"] == 0
            and current["potentially_unreported_model_usage_turns"] == 0 and after_cost is not None and before_cost is not None else None,
        "fixed_delay_stream_p99_reduction": reductions,
        "fixed_delay_stream_p99_reduction_at_least_30_percent_all_concurrencies": all(value >= .3 for value in reductions.values()) if {"1", "4", "8"} <= reductions.keys() else None,
        "real_agent_p99_reduction": agent_reduction,
        "real_agent_p99_reduction_at_least_30_percent": agent_reduction >= .3 if agent_reduction is not None else None,
        "real_pipeline_stage_total_p99_reduction": pipeline_reduction,
        "real_pipeline_stage_total_p99_reduction_at_least_30_percent": pipeline_reduction >= .3 if pipeline_reduction is not None else None,
        "agent_read_round_limit_below_1_percent": (current["routes"]["agent_read"]["round_limit"] / current["routes"]["agent_read"]["turns"]) < .01 if "agent_read" in current["routes"] else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", nargs="+", type=Path, required=True)
    parser.add_argument("--stream", nargs="+", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = summarize([json.loads(path.read_text()) for path in args.application], [json.loads(path.read_text()) for path in args.stream])
    report.update(generated_at=datetime.now(timezone.utc).isoformat(),
        analysis_code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        measurement_windows=[{"path": str(path), "started_at": json.loads(path.read_text())["started_at"],
            "report_file_written_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()} for path in args.application],
        inputs={"application": [str(path) for path in args.application], "stream": [str(path) for path in args.stream]},
        limitations=["Authored local business sandbox, real configured model; no user or production availability study.",
            "Case latency includes multiple turns and approval checks; pipeline stage total excludes commit/transport overhead.",
            "Fixed-delay model benchmark measures streaming infrastructure; its P99 is not real-provider latency.",
            "Wilson/Newcombe intervals treat runs as independent; repeated authored cases correlate, so intervals are descriptive.",
            "Frozen source/dataset hashes identify the measured code; build_sha may reflect concurrent commits to the parent checkout and is not an A/B grouping key.",
            "Actual intent sources are counted from traces; the raw runner's legacy intent_strategy label does not override them.",
            "Cost estimates use returned OpenAI-compatible SDK usage at peak prices; cancelled requests without usage, Jev and embedding charges are unknown and excluded."])
    if args.baseline:
        report["comparison"] = compare(json.loads(args.baseline.read_text()), report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report.get("comparison", report["live"]), ensure_ascii=False))


if __name__ == "__main__":
    main()
