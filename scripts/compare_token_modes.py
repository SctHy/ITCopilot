"""对比多组带 token 审计的评测报告：通过率、时延、Agent 用量与费用（deepseek-flash 价目）。

    python scripts/compare_token_modes.py base=<dir> disabled=<dir> low=<dir> ...

每个 <dir> 下应有 strata_v3.json、hard_v4.json、simulated_v4.json（缺哪个跳过哪个）。
费用只算产品自己的模型调用（agent + intent），不算 τ-bench 模拟用户（role=other）。
"""
from __future__ import annotations

import json
import pathlib
import sys

SUITES = ("strata_v3", "hard_v4", "simulated_v4")


def pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, round(q * (len(values) - 1)))] if values else 0


def row(path: pathlib.Path):
    data = json.loads(path.read_text())
    trials = data["trials"]
    roles = data["token_audit"]["by_role"]
    product = [roles[r] for r in ("agent", "intent") if r in roles]
    usage = {k: sum(r["usage"][k] for r in product) for k in ("prompt", "hit", "miss", "output", "reasoning")}
    cost = sum(r["cost_usd_off_peak"] for r in product)
    replies = [len(t["turns"][-1]["response"]["response"]) if t.get("turns") and isinstance(t["turns"], list)
               else len(next((m["content"] for m in reversed(t.get("transcript", [])) if m["role"] == "assistant"), ""))
               for t in trials]
    n = len(trials)
    return {"n": n, "passed": sum(t["passed"] for t in trials),
            "lat_p50": round(pct([t["latency_s"] for t in trials], .5), 2), "lat_p95": round(pct([t["latency_s"] for t in trials], .95), 2),
            "cost_per_trial_usd": round(cost / n, 6), "output_per_trial": round(usage["output"] / n), "reasoning_per_trial": round(usage["reasoning"] / n),
            "prompt_per_trial": round(usage["prompt"] / n), "miss_per_trial": round(usage["miss"] / n), "hit_rate": round(usage["hit"] / usage["prompt"], 3) if usage["prompt"] else 0,
            "agent_calls_per_trial": round(roles.get("agent", {}).get("calls", 0) / n, 2), "final_reply_chars_p50": pct(replies, .5)}


def main(argv):
    result = {}
    for arg in argv:
        name, _, folder = arg.partition("=")
        result[name] = {s: row(pathlib.Path(folder) / f"{s}.json") for s in SUITES if (pathlib.Path(folder) / f"{s}.json").exists()}
    keys = ("passed", "n", "lat_p50", "lat_p95", "cost_per_trial_usd", "output_per_trial", "reasoning_per_trial", "prompt_per_trial", "miss_per_trial", "hit_rate", "agent_calls_per_trial", "final_reply_chars_p50")
    for suite in SUITES:
        print(f"\n## {suite}")
        print("| mode | " + " | ".join(keys) + " |")
        print("|" + "---|" * (len(keys) + 1))
        for name, suites in result.items():
            if suite in suites:
                print(f"| {name} | " + " | ".join(str(suites[suite][k]) for k in keys) + " |")
    print("\n" + json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1:])
