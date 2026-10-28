"""Live full-application trials: HTTP chat -> frozen approval -> adapter -> state checks.

Uses the production lifespan with isolated Chroma/SQLite and a dedicated Redis URL.
Each case runs as a fresh identity whose employee_id is the 工号 in the first turn
(self-service policy); approvals are decided by separate approvers (bob, then carol when
the policy requires two), so separation of duties is exercised on every write.
No LLM judge is allowed to override a failed business assertion. All failures stay
in the denominator. Synthetic local business state is not production evidence.
"""

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import re
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from agents.agent_orchestrator import PLANNING_MODES
from evaluation.application import application, auth
from workflow.store import SEED_EMPLOYEES
from evaluation.reporting import ROOT, DATASETS_DIR, write_report


# The logged-in employee is the last 工号 the user states as their own, including corrections
# ("工号刚才说错了，改成1001", "我是1001"). ASCII only: \w would swallow "1002本人".
SELF_ID = re.compile(r"(?:工号(?:是)?|我是|改成)\s*([A-Za-z0-9]+)")


def requester_employee(case):
    ids = [m for turn in case["turns"] for m in SELF_ID.findall(turn)]
    return ids[-1] if ids else None


def upstream_model():
    try:
        import httpx
        health = httpx.get(os.getenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8082").rstrip("/") + "/health", timeout=5).json()
        return {"base_url": health.get("openai_base_url"), "model": health.get("openai_model")}
    except Exception as exc:
        return {"error": type(exc).__name__}


def percentile(values, q):
    return sorted(values)[min(len(values) - 1, int((len(values) - 1) * q))] if values else 0


async def trial(http, api, case, mode, repeat):
    unique = uuid.uuid4().hex
    result = {"case_id": case["id"], "stratum": case.get("stratum", "v2"), "planning": mode, "repeat": repeat, "turns": [], "checks": {}}
    employee = case.get("employee_id", "1001")
    login = case.get("requester_employee_id") or requester_employee(case) or employee
    requester = auth("alice", subject=f"eval:{unique}", employee_id=login, department="engineering")
    reader = auth("grace")  # auditor: reads business state without being party to the request
    # Unique identities isolate working/episodic memory; sandbox rows reset between sequential cases.
    api._workflow.reset_directory([e for e, _, _ in SEED_EMPLOYEES])
    start = time.monotonic()
    try:
        for index, message in enumerate(case["turns"]):
            response = await http.post(
                "/chat",
                json={"message": message, "conv_id": unique, "client_request_id": f"{unique}-{index}"},
                headers=requester,
            )
            response.raise_for_status()
            body = response.json()
            trace = (await http.get("/trace/" + body["request_id"], headers=requester)).json()
            result["turns"].append({"response": body, "trace": trace})
        checks, outcome = result["checks"], case["outcome"]
        checks["all_turns_completed"] = all(
            t["response"]["status"] in {"success", "partial"} for t in result["turns"]
        )
        # Evidence per domain: each group lists acceptable tools; any successful call satisfies it.
        used = [x["tool_name"] for t in result["turns"] for x in t["trace"].get("trace", {}).get("tool_traces", []) if x.get("success")]
        result["tools_called"] = used
        for domain, group in case.get("evidence", {}).items():
            checks[f"evidence_{domain}"] = any(tool in used for tool in group)
        for tool in case.get("forbid_tools", []):
            checks[f"not_{tool}"] = tool not in used
        if case.get("first_no_approval"):
            checks["clarified_before_write"] = not result["turns"][0]["response"][
                "approval_required"
            ]
        before = (await http.get(f"/business/employees/{employee}", headers=reader)).json()
        result["business_before"] = before
        checks["no_effect_before_approval"] = (
            before.get("grants") == []
            and before.get("password_version") == 0
            and before.get("locked") == 1
            and before.get("tickets") == []
        )
        approval_id = body.get("approval_id")
        if outcome in {"grant", "unlocked", "password", "ticket", "reject", "execution_failed", "multi"}:
            checks["approval_created"] = bool(approval_id)
            if approval_id:
                path = "/approvals/" + approval_id
                frozen = (await http.get(path, headers=requester)).json()
                decision = {
                    "decision": "rejected" if outcome == "reject" else "approved",
                    "payload_hash": frozen["payload_hash"],
                }
                approvers = [auth("bob"), auth("carol")][: max(1, frozen["approvals_required"]) if outcome != "reject" else 1]
                for approver in approvers:
                    executed = await http.post(path, json=decision, headers=approver)
                    executed.raise_for_status()
                result["approval"] = executed.json()
                duplicate = (await http.post(path, json=decision, headers=approvers[-1])).json()
                checks["duplicate_decision_stable"] = duplicate == result["approval"]
                checks["requester_cannot_self_approve"] = (await http.post(path, json=decision, headers=requester)).status_code == 403
                after = (await http.get(f"/business/employees/{employee}", headers=reader)).json()
                result["business_after"] = after
                if outcome == "grant":
                    checks["correct_grant"] = any(
                        g["system"] == case["system"].casefold() and g["role"] == case["role"].casefold()
                        for g in after["grants"]
                    )
                    checks["expiry_scheduled"] = bool(result["approval"].get("revocations"))
                    checks["no_extra_grants"] = len(after["grants"]) == 1
                elif outcome == "password":
                    checks["one_reset"] = after["password_version"] == 1
                elif outcome == "unlocked":
                    checks["unlocked"] = after["locked"] == 0
                elif outcome == "ticket":
                    checks["ticket_persisted"] = len(after["tickets"]) == 1
                elif outcome == "multi":
                    # Several effects in one request; ticket count is a minimum ("分别建单" may yield one per fault).
                    effects = case["effects"]
                    if "grant" in effects:
                        checks["correct_grant"] = any(g["system"] == effects["grant"]["system"] and g["role"] == effects["grant"]["role"] for g in after["grants"])
                    if "tickets" in effects:
                        checks["ticket_persisted"] = len(after["tickets"]) >= effects["tickets"]
                    if effects.get("password"):
                        checks["one_reset"] = after["password_version"] == 1
                    if effects.get("unlocked"):
                        checks["unlocked"] = after["locked"] == 0
                elif outcome == "reject":
                    checks["rejected_without_effect"] = (
                        after == before and result["approval"]["execution"] == "not_executed"
                    )
                else:
                    checks["failed_without_effect"] = (
                        after == before and result["approval"]["execution"] == "failed"
                    )
        elif outcome == "tools":
            checks["required_tools"] = set(case.get("tools", [])) <= set(used)
            checks["no_write"] = not approval_id
        elif outcome == "citation":
            checks["retrieved"] = body["knowledge_used"]
            checks["expected_citation"] = case["title"] in body["response"]
            # Citation presence is NOT a claim of faithfulness.
        else:
            checks["no_write"] = not approval_id
        result["passed"] = bool(checks) and all(checks.values())
    except Exception as exc:
        result.update(passed=False, error_type=type(exc).__name__)
    result["latency_s"] = time.monotonic() - start
    result["agent_tokens"] = sum(
        sum(t["response"].get("token_usage", {}).values()) for t in result["turns"]
    )
    return result


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--redis-url", default="redis://127.0.0.1:16379/0")
    p.add_argument("--limit", type=int, default=24)
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--planning", default="single,bounded")
    p.add_argument("--dataset", default=str(DATASETS_DIR / "application_v2.json"))
    p.add_argument("--output", default="application_live.json")
    p.add_argument("--cases", default="", help="comma-separated case ids to run (debugging subset)")
    args = p.parse_args()
    load_dotenv(ROOT / ".env")
    source = pathlib.Path(args.dataset)
    cases = json.loads(source.read_text())[: args.limit]
    if args.cases:
        wanted = set(args.cases.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    started_at = datetime.now(timezone.utc).isoformat()
    code = hashlib.sha256()
    for folder in ("api", "agents", "core", "memory", "tooling", "evaluation"):
        for file in sorted((ROOT / folder).glob("*.py")):
            code.update(str(file.relative_to(ROOT)).encode() + file.read_bytes())
    code_sha256 = code.hexdigest()
    corpus_path = DATASETS_DIR / "rag_v2_corpus.json"
    documents = json.loads(corpus_path.read_text()) if corpus_path.exists() else []
    rows = []
    async with application(redis_url=args.redis_url, documents=documents) as (http, api):
        for repeat in range(args.repeats):
            for case in cases:
                # Alternate paired order to avoid a fixed warm-cache/temporal advantage.
                modes = args.planning.split(",")
                shift = (repeat + cases.index(case)) % len(modes)
                modes = modes[shift:] + modes[:shift]  # rotate order: no strategy always runs first/cold
                for mode in modes:
                    if mode not in PLANNING_MODES:
                        raise ValueError(f"planning must be one of {PLANNING_MODES}")
                    api._orchestrator.planning_mode = mode
                    api._tool_manager.invalidate_cache("knowledge_search")
                    row = await trial(http, api, case, mode, repeat)
                    rows.append(row)
                    print(f"{mode} {case['id']}: {row['passed']}", flush=True)
                    write_report(
                        args.output.replace(".json", "_partial.json"),
                        {"status": "running", "trials": rows},
                        echo=False,
                    )
        source_calls = api._orchestrator._intent_recognizer.metrics()
        intent_strategy = api._orchestrator._intent_recognizer.strategy
    summary = {}
    for mode in args.planning.split(","):
        group = [r for r in rows if r["planning"] == mode]
        summary[mode] = {
            "trials": len(group),
            "passed": sum(r["passed"] for r in group),
            "success_rate": sum(r["passed"] for r in group) / max(1, len(group)),
            "latency_p50_s": statistics.median(r["latency_s"] for r in group),
            "latency_p95_s": percentile([r["latency_s"] for r in group], 0.95),
            "agent_tokens_median": statistics.median(r["agent_tokens"] for r in group),
        }
    report = {
        "suite": "application-v2",
        "status": "complete",
        "model": os.getenv("ANTHROPIC_MODEL"),
        # ANTHROPIC_MODEL is the name the app sends to the gateway; record what actually reached the provider.
        "upstream": upstream_model(),
        "started_at": started_at,
        "code_sha256": code_sha256,
        "intent_strategy": intent_strategy,
        "dataset": str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else source.name,
        "dataset_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "source_calls": source_calls,
        "summary": summary,
        "trials": rows,
        "limitations": "Authored local sandbox tasks; real model/Redis/Chroma/SQLite and ASGI HTTP handlers; no enterprise identity integration. Token totals cover agent runs only, excluding intent/rewrite/rerank. Single mode retains role-specific tools. Not a user study.",
    }
    write_report(args.output, report, echo=False)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
