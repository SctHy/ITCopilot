"""Run τ-bench-style simulated-user tasks against every planning strategy.

    python scripts/evaluate_simulated.py --strategies generalist,bounded,bounded_all,single --output simulated_v4_run1.json
    python scripts/compare_strategies.py evaluation/reports/simulated_v4_run1.json evaluation/reports/simulated_v4_run2.json

Each run is one process (the intent cache makes in-process repeats dependent); pass^k is
computed across runs by compare_strategies (a task passes only if it passes in every run).
The user simulator uses the same upstream model as the agent; its tokens are reported
separately from agent tokens.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import sys
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402

from core.llm import build_model, complete_text  # noqa: E402
from evaluation.application import application, auth  # noqa: E402
from evaluation.reporting import DATASETS_DIR, ROOT, write_report  # noqa: E402
from evaluation.simulated import STOP, generate_tasks, grade, simulator_flags, user_prompt  # noqa: E402
from workflow.store import SEED_EMPLOYEES  # noqa: E402

DATASET = DATASETS_DIR / "simulated_v4.json"


async def run_task(http, api, task, mode, user_llm, max_turns):
    unique = uuid.uuid4().hex
    requester = auth("alice", subject=f"eval:{unique}", employee_id=task["employee_id"], department="engineering")
    auditor = auth("grace")
    api._workflow.reset_directory([e for e, _, _ in SEED_EMPLOYEES])
    transcript, tool_calls, agent_tokens, user_calls, ended = [], [], 0, 0, "max_turns"
    started = time.monotonic()
    result = {"case_id": task["id"], "stratum": task["tier"], "planning": mode, "repeat": 0}
    try:
        message = task["opening"]
        for index in range(max_turns):
            response = await http.post("/chat", json={"message": message, "conv_id": unique, "client_request_id": f"{unique}-{index}"}, headers=requester)
            response.raise_for_status()
            body = response.json()
            trace = (await http.get("/trace/" + body["request_id"], headers=requester)).json().get("trace", {})
            tool_calls += [{k: c.get(k) for k in ("tool_name", "input", "success")} for c in trace.get("tool_traces", [])]
            agent_tokens += sum(body.get("token_usage", {}).values())
            transcript += [{"role": "user", "content": message}, {"role": "assistant", "content": body["response"]}]
            reply = (await complete_text(user_llm, user_prompt(task, transcript), max_tokens=200, request_timeout=60)).text.strip()
            user_calls += 1
            if STOP in reply or not reply:
                ended = "user_stop"
                break
            message = reply
        # Approvers decide everything the agent filed; state is graded after execution.
        approvals = (await http.get("/approvals", params={"status": "pending"}, headers=requester)).json()["items"]
        for approval in approvals:
            path = "/approvals/" + approval["approval_id"]
            for approver in ("bob", "carol")[: max(1, approval["approvals_required"])]:
                (await http.post(path, json={"decision": "approved", "payload_hash": approval["payload_hash"]}, headers=auth(approver))).raise_for_status()
        superseded = len((await http.get("/approvals", params={"status": "superseded"}, headers=requester)).json()["items"])
        state = (await http.get(f"/business/employees/{task['employee_id']}", headers=auditor)).json()
        agent_text = "\n".join(m["content"] for m in transcript if m["role"] == "assistant")
        checks = grade(task, state, tool_calls, agent_text, approvals)
        result.update(checks=checks, passed=all(checks.values()), approvals=len(approvals), superseded=superseded, simulator_flags=simulator_flags(task, transcript), state={k: state[k] for k in ("grants", "tickets", "password_version", "locked")})
    except Exception as exc:
        result.update(checks={"no_exception": False}, passed=False, error_type=f"{type(exc).__name__}: {exc}"[:300])
    result.update(turns=len(transcript) // 2, ended=ended, agent_tokens=agent_tokens, user_calls=user_calls,
                  latency_s=time.monotonic() - started, transcript=transcript, tool_calls=tool_calls)
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:16379/0")
    parser.add_argument("--strategies", default="generalist,bounded,bounded_all,single")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--tasks", default="", help="comma-separated task ids (debug subset)")
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--output", default="simulated_v4.json")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if not DATASET.exists():
        DATASET.write_text(json.dumps(generate_tasks(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tasks = json.loads(DATASET.read_text(encoding="utf-8"))[: args.limit]
    if args.tasks:
        wanted = set(args.tasks.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    user_llm = build_model(os.getenv("ANTHROPIC_API_KEY", "sk-local"), os.getenv("ANTHROPIC_BASE_URL"), os.getenv("ANTHROPIC_MODEL", "deepseek-chat"))
    from scripts.evaluate_application import upstream_model

    modes = args.strategies.split(",")
    rows = []
    async with application(redis_url=args.redis_url, documents=json.loads((DATASETS_DIR / "rag_v2_corpus.json").read_text())) as (http, api):
        for index, task in enumerate(tasks):
            shift = index % len(modes)
            for mode in modes[shift:] + modes[:shift]:
                api._orchestrator.planning_mode = mode
                api._tool_manager.invalidate_cache("knowledge_search")
                row = await run_task(http, api, task, mode, user_llm, args.max_turns)
                rows.append(row)
                print(f"{mode} {task['id']}: {row['passed']} turns={row['turns']} ended={row['ended']}", flush=True)
                write_report(args.output.replace(".json", "_partial.json"), {"status": "running", "trials": rows}, echo=False)
        planner = api._orchestrator._planner.metrics if api._orchestrator._planner else {}
    summary = {}
    for mode in modes:
        group = [r for r in rows if r["planning"] == mode]
        summary[mode] = {"trials": len(group), "passed": sum(r["passed"] for r in group), "success_rate": round(sum(r["passed"] for r in group) / max(1, len(group)), 3)}
    write_report(args.output, {
        "suite": "simulated-v4", "status": "complete", "started_at": datetime.now(timezone.utc).isoformat(),
        "upstream": upstream_model(), "dataset": DATASET.name, "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest()[:16], "max_turns": args.max_turns, "planner": planner,
        # Identifies the simulator instructions: runs are only comparable under the same prompt.
        "simulator_prompt_sha256": hashlib.sha256(user_prompt(json.loads(DATASET.read_text())[0], []).encode()).hexdigest()[:16],
        "summary": summary, "trials": rows,
        "limitations": "Authored compositional tasks and simulator prompts in the style of τ-bench; same model plays the user; local sandbox state.",
    }, echo=False)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
