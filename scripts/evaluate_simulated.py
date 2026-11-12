"""Run τ-bench-style simulated-user tasks against the agent.

    python scripts/evaluate_simulated.py --output simulated_v4_run1.json

Each run is one process (the intent cache makes in-process repeats dependent); pass^k is
computed across runs (a task passes only if it passes in every run).
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
from evaluation.reporting import DATASETS_DIR, ROOT, billable_tokens, write_report  # noqa: E402
from evaluation.states import TRIAL  # noqa: E402
from evaluation.simulated import STOP, generate_tasks, grade, simulator_flags, user_prompt  # noqa: E402
from workflow.store import SEED_EMPLOYEES  # noqa: E402

DATASET = DATASETS_DIR / "simulated_v4.json"


async def run_task(http, state, task, user_llm, max_turns):
    unique = uuid.uuid4().hex
    requester = auth("alice", subject=f"eval:{unique}", employee_id=task["employee_id"], department="engineering")
    auditor = auth("grace")
    state.workflow.reset_directory([e for e, _, _ in SEED_EMPLOYEES])
    transcript, tool_calls, agent_tokens, user_calls, ended = [], [], 0, 0, "max_turns"
    flow = TRIAL.start()
    started = time.monotonic()
    result = {"case_id": task["id"], "stratum": task["tier"], "repeat": 0}
    try:
        message = task["opening"]
        for index in range(max_turns):
            response = await http.post("/chat", json={"message": message, "conv_id": unique, "client_request_id": f"{unique}-{index}"}, headers=requester)
            response.raise_for_status()
            body = response.json()
            trace = (await http.get("/trace/" + body["request_id"], headers=requester)).json().get("trace", {})
            tool_calls += [{k: c.get(k) for k in ("tool_name", "input", "success")} for c in trace.get("tool_traces", [])]
            if body.get("knowledge_used") and not any(c["tool_name"] == "search_knowledge_base" for c in trace.get("tool_traces", [])):
                # Jev 路径与意图识别并行预取知识并附进本轮消息，Agent 不再调用检索工具；预取命中同样算检索。
                tool_calls.append({"tool_name": "search_knowledge_base", "input": {"prefetched": True}, "success": True})
            agent_tokens += billable_tokens(body.get("token_usage"))
            transcript += [{"role": "user", "content": message}, {"role": "assistant", "content": body["response"]}]
            reply = (await complete_text(user_llm, user_prompt(task, transcript), max_tokens=200, request_timeout=60)).text.strip()
            user_calls += 1
            if STOP in reply or not reply:
                ended = flow.send("stop")
                break
            message = reply
        if flow.state == "dialogue":
            ended = flow.send("limit")
        flow.send("approve")
        # The requester submits drafts left on their cards (ADR 0020), then approvers decide
        # everything filed; state is graded after execution.
        for draft in (await http.get("/approvals", params={"status": "draft"}, headers=requester)).json()["items"]:
            (await http.post(f"/approvals/{draft['approval_id']}/confirm", json={"payload_hash": draft["payload_hash"]}, headers=requester)).raise_for_status()
        approvals = (await http.get("/approvals", params={"status": "pending"}, headers=requester)).json()["items"]
        for approval in approvals:
            path = "/approvals/" + approval["approval_id"]
            for approver in ("bob", "carol")[: max(1, approval["approvals_required"])]:
                (await http.post(path, json={"decision": "approved", "payload_hash": approval["payload_hash"]}, headers=auth(approver))).raise_for_status()
        superseded = len((await http.get("/approvals", params={"status": "superseded"}, headers=requester)).json()["items"])
        state = (await http.get(f"/business/employees/{task['employee_id']}", headers=auditor)).json()
        agent_text = "\n".join(m["content"] for m in transcript if m["role"] == "assistant")
        flow.send("grade")
        checks = grade(task, state, tool_calls, agent_text, approvals)
        result.update(checks=checks, passed=all(checks.values()), approvals=len(approvals), superseded=superseded, simulator_flags=simulator_flags(task, transcript), state={k: state[k] for k in ("grants", "tickets", "password_version", "locked")})
        flow.send("complete")
    except Exception as exc:
        flow.send("fail")
        result.update(checks={"no_exception": False}, passed=False, error_type=f"{type(exc).__name__}: {exc}"[:300])
    result.update(phase=flow.state, turns=len(transcript) // 2, ended=ended, agent_tokens=agent_tokens, user_calls=user_calls,
                  latency_s=time.monotonic() - started, transcript=transcript, tool_calls=tool_calls)
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:16379/0")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--tasks", default="", help="comma-separated task ids (debug subset)")
    parser.add_argument("--max-turns", type=int, default=8)
    parser.add_argument("--output", default="simulated_v4.json")
    parser.add_argument("--user-model", default=os.getenv("SIM_USER_MODEL", ""),
                        help="模拟用户使用的模型（同一 OpenAI-compatible 服务）；默认与被测 Agent 相同")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if not DATASET.exists():
        DATASET.write_text(json.dumps(generate_tasks(), ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tasks = json.loads(DATASET.read_text(encoding="utf-8"))[: args.limit]
    if args.tasks:
        wanted = set(args.tasks.split(","))
        tasks = [t for t in tasks if t["id"] in wanted]
    user_model = args.user_model or os.getenv("OPENAI_MODEL", "deepseek-chat")
    user_llm = build_model(os.getenv("OPENAI_API_KEY", "sk-local"), os.getenv("OPENAI_BASE_URL"), user_model)
    from scripts.evaluate_application import upstream_model

    rows = []
    from evaluation.token_audit import TokenAudit, summarize
    audit = TokenAudit().__enter__()
    async with application(redis_url=args.redis_url, documents=json.loads((DATASETS_DIR / "rag_v2_corpus.json").read_text())) as (http, state):
        for task in tasks:
            state.tool_manager.invalidate_cache("knowledge_search")
            before = len(audit.records)
            row = await run_task(http, state, task, user_llm, args.max_turns)
            row["model_usage"] = summarize(audit.records[before:])
            rows.append(row)
            print(f"{task['id']}: {row['passed']} turns={row['turns']} ended={row['ended']}", flush=True)
            write_report(args.output.replace(".json", "_partial.json"), {"status": "running", "trials": rows}, echo=False)
    summary = {"trials": len(rows), "passed": sum(r["passed"] for r in rows), "success_rate": round(sum(r["passed"] for r in rows) / max(1, len(rows)), 3)}
    write_report(args.output, {
        "suite": "simulated-v4", "status": "complete", "started_at": datetime.now(timezone.utc).isoformat(),
        "upstream": upstream_model(), "user_model": user_model, "dataset": DATASET.name, "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest()[:16], "max_turns": args.max_turns,
        # Identifies the simulator instructions: runs are only comparable under the same prompt.
        "simulator_prompt_sha256": hashlib.sha256(user_prompt(json.loads(DATASET.read_text())[0], []).encode()).hexdigest()[:16],
        "summary": summary, "agent_thinking": os.getenv("IT_COPILOT_AGENT_THINKING", "disabled"),
        "token_audit": audit.summary(), "trials": rows,
        "limitations": "Authored compositional tasks and simulator prompts in the style of τ-bench; " + ("same model plays the user" if user_model == os.getenv("OPENAI_MODEL") else f"user simulated by {user_model}") + "; local sandbox state.",
    }, echo=False)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
