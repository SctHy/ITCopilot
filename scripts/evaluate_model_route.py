"""在完整应用用例上强制指定调用角色的候选；业务断言保持原判分标准。"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.model_router import ROUTES, ModelRouter
from evaluation.route_qualification import PROBES, qualify
from dotenv import load_dotenv
from evaluation.attribution import wilson
from evaluation.reporting import percentile


def from_application_reports(paths, route, model, router, repeats):
    rows, sources, calls = [], [], 0
    for path in paths:
        source = pathlib.Path(path)
        report = json.loads(source.read_text())
        if report["status"] != "complete":
            raise ValueError(f"incomplete application report: {source}")
        repeat_sets = {}
        for row in report["trials"]:
            repeat_sets.setdefault(row["case_id"], set()).add(row["repeat"])
            for turn in row["turns"]:
                versions = turn["trace"].get("trace", {}).get("observability", {}).get("versions", {})
                if versions.get("route") == route:
                    if versions["model"] != model:
                        raise ValueError(f"{source}: {route} used {versions['model']} instead of {model}")
                    calls += 1
            rows.append(row)
        if not repeat_sets or any(len(found) < repeats for found in repeat_sets.values()):
            raise ValueError(f"{source}: fewer than {repeats} repetitions for one or more cases")
        sources.append({"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "dataset_sha256": report["dataset_sha256"], "code_sha256": report["code_sha256"]})
    if not calls:
        raise ValueError(f"source application reports never called {route}/{model}")
    passed = sum(row["passed"] for row in rows)
    costs = sum(turn["trace"].get("trace", {}).get("observability", {}).get("versions", {}).get("cost_usd", 0.0)
                for row in rows for turn in row["turns"])
    return {"route": route, "model": model, "model_config_sha256": router.model_fingerprint(model),
        "status": "complete", "kind": "real_model", "evaluation_kind": "application_business_oracle",
        "repeats": repeats, "trials": len(rows), "passed": passed, "success_rate": passed / len(rows),
        "wilson": wilson(passed, len(rows)), "p95_s": percentile([row["latency_s"] for row in rows], .95),
        "cost_per_success_usd": costs / passed if passed else None, "route_calls": calls,
        "source_reports": sources,
        "dataset_sha256": hashlib.sha256(json.dumps([source["dataset_sha256"] for source in sources]).encode()).hexdigest(),
        "limitations": "Reuses complete real application trials and their original business oracle; every failed case remains in the denominator. Role candidate identity is verified in actual turn traces; latency/cost cover the complete case, not this role alone."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--route", required=True, choices=ROUTES)
    parser.add_argument("--model", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--dataset", default="application_strata_v3")
    parser.add_argument("--redis-url", default="redis://127.0.0.1:16379/0")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--application-report", action="append", default=[], help="reuse completed real application evidence; verify actual role and model in every trace")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    report = ROOT / f"evaluation/reports/routing/{args.route}__{args.model}.json"
    if args.application_report:
        router = ModelRouter()
        measured = from_application_reports(args.application_report, args.route, args.model, router, args.repeats)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(measured, ensure_ascii=False, indent=2) + "\n")
        print(report)
        return
    if args.route in PROBES:
        load_dotenv(ROOT / ".env")
        router = ModelRouter()
        measured = asyncio.run(qualify(router, args.route, args.model, args.repeats))
        measured["model_config_sha256"] = router.model_fingerprint(args.model)
        measured["code_sha256"] = hashlib.sha256((ROOT / "evaluation/route_qualification.py").read_bytes()).hexdigest()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(measured, ensure_ascii=False, indent=2) + "\n")
        print(report)
        return
    application_report = report.with_name(report.stem + "_application.json")
    command = [sys.executable, str(ROOT / "scripts/evaluate_application.py"), "--dataset", args.dataset,
        "--repeats", str(args.repeats), "--model-route", args.route, "--model", args.model,
        "--redis-url", args.redis_url, "--output", str(application_report)]
    if args.limit is not None:
        command += ["--limit", str(args.limit)]
    subprocess.run(command, check=True, cwd=ROOT)
    result = json.loads(application_report.read_text())
    summary = result["summary"]
    route_calls = result["route_calls"].get(args.route, 0)
    router = ModelRouter()
    measured = {"route": args.route, "model": args.model, "model_config_sha256": router.model_fingerprint(args.model), "status": result["status"] if route_calls else "unexercised", "kind": "real_model",
        "route_calls": route_calls,
        "repeats": args.repeats, "dataset": result["dataset"], "dataset_sha256": result["dataset_sha256"],
        "code_sha256": result["code_sha256"], "trials": summary["trials"], "passed": summary["passed"],
        "success_rate": summary["success_rate"], "wilson": wilson(summary["passed"], summary["trials"]),
        "p95_s": summary["latency_p95_s"], "cost_per_success_usd": summary["cost_per_success_usd"],
        "application_report": str(application_report.relative_to(ROOT)),
        "limitations": "End-to-end application success with one role forced; synthetic sandbox tasks, no production traffic. Costs cover agent turns at configured peak estimates."}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(measured, ensure_ascii=False, indent=2) + "\n")
    print(report)


if __name__ == "__main__":
    main()
