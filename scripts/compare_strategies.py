"""Paired comparison of planning strategies on one application report.

Every case runs under each strategy in the same process, so comparisons are paired:
for strategies A and B, b = cases A passed and B failed, c = the reverse; the exact
McNemar test uses only those discordant cases. Pass rates carry Wilson 95% intervals.
Repeats of the same case share the intent cache within a run, so they are not
independent samples; with --repeats > 1 a case counts as passed only if every
repeat passed (strict) and the repeat rate is reported separately.

    python scripts/compare_strategies.py evaluation/reports/application_strata_v3_run1.json [run2.json ...]

Several reports (independent processes) are merged; each counts as one repeat.
"""
from __future__ import annotations

import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path


def wilson(passed: int, n: int, z: float = 1.96):
    if not n:
        return (0.0, 0.0)
    p = passed / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (round(centre - half, 3), round(centre + half, 3))


def mcnemar_exact(b: int, c: int) -> float:
    n = b + c
    if not n:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 4)


def analyse(report):
    modes = list(report["summary"])
    by_case = defaultdict(lambda: defaultdict(list))
    for trial in report["trials"]:
        by_case[(trial.get("stratum", "v2"), trial["case_id"])][trial["planning"]].append(trial)
    order = ["S", "C2", "C3", "M", "N", "T1", "T2", "T3", "P"]
    strata = sorted({s for s, _ in by_case}, key=lambda s: (order.index(s) if s in order else len(order), s))

    def case_pass(trials):
        return all(t["passed"] for t in trials)

    out = {"modes": modes, "strata": {}, "paired": {}, "evidence": {}, "failures": {}}
    for stratum in strata + ["ALL"]:
        keys = [k for k in by_case if stratum == "ALL" or k[0] == stratum]
        row = {"cases": len(keys)}
        for mode in modes:
            trials = [t for k in keys for t in by_case[k][mode]]
            passed = sum(case_pass(by_case[k][mode]) for k in keys)
            row[mode] = {
                "passed": passed, "rate": round(passed / max(1, len(keys)), 3), "wilson95": wilson(passed, len(keys)),
                "trial_rate": round(sum(t["passed"] for t in trials) / max(1, len(trials)), 3),
                "tokens_median": statistics.median([t["agent_tokens"] for t in trials]) if trials else 0,
                "latency_p50_s": round(statistics.median([t["latency_s"] for t in trials]), 2) if trials else 0,
                # Simulated-user reports count turns; application reports store the turn list.
                "turns_mean": round(statistics.mean([t["turns"] if isinstance(t["turns"], int) else len(t["turns"]) for t in trials]), 2) if trials and "turns" in trials[0] else None,
            }
        out["strata"][stratum] = row
    for a, b in combinations(modes, 2):
        per = {}
        for stratum in strata + ["ALL"]:
            keys = [k for k in by_case if stratum == "ALL" or k[0] == stratum]
            only_a = sorted(k[1] for k in keys if case_pass(by_case[k][a]) and not case_pass(by_case[k][b]))
            only_b = sorted(k[1] for k in keys if case_pass(by_case[k][b]) and not case_pass(by_case[k][a]))
            per[stratum] = {f"only_{a}": only_a, f"only_{b}": only_b, "mcnemar_p": mcnemar_exact(len(only_a), len(only_b))}
        out["paired"][f"{a}_vs_{b}"] = per
    for mode in modes:
        hits, totals = Counter(), Counter()
        fails = Counter()
        for k, runs in by_case.items():
            for t in runs[mode]:
                for name, ok in t["checks"].items():
                    if name.startswith("evidence_"):
                        totals[name] += 1
                        hits[name] += ok
                    if not ok:
                        fails[name] += 1
                if t.get("error_type"):
                    fails["exception:" + t["error_type"]] += 1
        out["evidence"][mode] = {name: round(hits[name] / totals[name], 3) for name in sorted(totals)}
        out["failures"][mode] = dict(fails.most_common())
    return out


def markdown(result) -> str:
    modes = result["modes"]
    lines = ["| 分层 | 场景 | " + " | ".join(f"{m} pass^k（95% CI）/ pass^1" for m in modes) + " | " + " | ".join(f"{m} token 中位" for m in modes) + " |",
             "|---" * (2 + 2 * len(modes)) + "|"]
    for stratum, row in result["strata"].items():
        cells = [f"{row[m]['passed']}/{row['cases']}（{row[m]['wilson95'][0]:.2f}–{row[m]['wilson95'][1]:.2f}）/ {row[m]['trial_rate']:.2f}" for m in modes]
        tokens = [f"{row[m]['tokens_median']:.0f}" for m in modes]
        lines.append(f"| {stratum} | {row['cases']} | " + " | ".join(cells) + " | " + " | ".join(tokens) + " |")
    lines.append("")
    for pair, per in result["paired"].items():
        a, b = pair.split("_vs_")
        allrow = per["ALL"]
        lines.append(f"- {a} vs {b}：仅 {a} 通过 {len(allrow['only_' + a])} 个，仅 {b} 通过 {len(allrow['only_' + b])} 个，McNemar p={allrow['mcnemar_p']}")
    return "\n".join(lines)


def merge(paths, exclude_flagged=False, exclude_errored=False):
    """Reports may be split per strategy and per run (…_<mode>_run<N>.json); the run number
    in the file name is the repeat index, otherwise the file position is."""
    import re

    trials, modes = [], []
    for index, path in enumerate(paths):
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        match = re.search(r"_run(\d+)", Path(path).stem)
        run = int(match.group(1)) if match else index
        for mode in report["summary"]:
            if mode not in modes:
                modes.append(mode)
        for trial in report["trials"]:
            if exclude_flagged and trial.get("simulator_flags"):
                continue
            trials.append({**trial, "repeat": run})
    # Drop the whole case for every strategy so the comparison stays paired.
    everything = [t for path in paths for t in json.loads(Path(path).read_text(encoding="utf-8"))["trials"]]
    if exclude_flagged:
        trials = [t for t in trials if t["case_id"] not in {t["case_id"] for t in everything if t.get("simulator_flags")}]
    if exclude_errored:  # provider failures (rate limit, quota) say nothing about the strategy
        trials = [t for t in trials if t["case_id"] not in {t["case_id"] for t in everything if t.get("error_type")}]
    return {**report, "summary": {m: {} for m in modes}, "trials": trials}


def main() -> int:
    exclude = "--exclude-simulator-flags" in sys.argv
    errored = "--exclude-errored" in sys.argv
    paths = [Path(p) for p in sys.argv[1:] if not p.startswith("--")]
    report = merge(paths, exclude_flagged=exclude, exclude_errored=errored)
    result = analyse(report)
    all_trials = merge(paths)["trials"]
    result["simulator_flag_rate"] = round(sum(bool(t.get("simulator_flags")) for t in all_trials) / max(1, len(all_trials)), 3)
    result["excluded_flagged_cases"] = exclude
    result["excluded_errored_cases"] = sorted({t["case_id"] for t in all_trials if t.get("error_type")}) if errored else []
    result["source"] = [p.name for p in paths]
    result["runs"] = len(paths)
    result["model"] = report.get("upstream")
    import re
    stem = re.sub(r"_(generalist|bounded_all|bounded|single)$", "", paths[0].stem.split("_run")[0])
    out = paths[0].with_name(stem + "_comparison" + ("_clean" if exclude else "") + ("_completed" if errored else "") + ".json")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(markdown(result))
    print("\nevidence coverage:", json.dumps(result["evidence"], ensure_ascii=False))
    print("top failed checks:", json.dumps({m: dict(list(f.items())[:6]) for m, f in result["failures"].items()}, ensure_ascii=False))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
