from __future__ import annotations


import hashlib

import json

import pathlib


from tests.support.attribution import wilson

from tests.support.reporting import percentile


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
                versions = (
                    turn["trace"].get("trace", {}).get("observability", {}).get("versions", {})
                )
                if versions.get("route") == route:
                    if versions["model"] != model:
                        raise ValueError(
                            f"{source}: {route} used {versions['model']} instead of {model}"
                        )
                    calls += 1
            rows.append(row)
        if not repeat_sets or any(len(found) < repeats for found in repeat_sets.values()):
            raise ValueError(f"{source}: fewer than {repeats} repetitions for one or more cases")
        sources.append(
            {
                "path": str(source),
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "dataset_sha256": report["dataset_sha256"],
                "code_sha256": report["code_sha256"],
            }
        )
    if not calls:
        raise ValueError(f"source application reports never called {route}/{model}")
    passed = sum(row["passed"] for row in rows)
    costs = sum(
        turn["trace"]
        .get("trace", {})
        .get("observability", {})
        .get("versions", {})
        .get("cost_usd", 0.0)
        for row in rows
        for turn in row["turns"]
    )
    return {
        "route": route,
        "model": model,
        "model_config_sha256": router.model_fingerprint(model),
        "status": "complete",
        "kind": "real_model",
        "evaluation_kind": "application_business_oracle",
        "repeats": repeats,
        "trials": len(rows),
        "passed": passed,
        "success_rate": passed / len(rows),
        "wilson": wilson(passed, len(rows)),
        "p95_s": percentile([row["latency_s"] for row in rows], 0.95),
        "cost_per_success_usd": costs / passed if passed else None,
        "route_calls": calls,
        "source_reports": sources,
        "dataset_sha256": hashlib.sha256(
            json.dumps([source["dataset_sha256"] for source in sources]).encode()
        ).hexdigest(),
        "limitations": "Reuses complete real application trials and their original business oracle; every failed case remains in the denominator. Role candidate identity is verified in actual turn traces; latency/cost cover the complete case, not this role alone.",
    }
