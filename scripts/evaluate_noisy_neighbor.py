"""Noisy neighbour: does tenant A flooding the service slow tenant B down? (ADR 0035)

The real application in-process (temporary SQLite, fakeredis), a model substitute that sleeps a fixed
time per call and an execution adapter that sleeps per call, so the numbers isolate the scheduling,
not model or network variance. Three scenarios, each ``--duration`` seconds:

- ``baseline``: only B. Two B users each send a turn, wait for it, pause, repeat; B approvals are
  decided at a fixed cadence and timed from decision to execution settled.
- ``noisy_protected``: the same B traffic while A sends turns at 10x its ``agent_turn`` quota and has
  a backlog of approved actions. Tenant rate limit and fair claiming on.
- ``noisy_unprotected``: the same flood with both off (oldest-first claiming, as before ADR 0035).

Experiment thresholds (not a release gate), for ``noisy_protected`` against ``baseline``: B's turn
p95 grows by at most 20 %, B gets no 429, B's approval execution p95 is at most 1.5x.

The baseline and unprotected scenarios patch the tenant limit out and claiming back to oldest-first.
Load generator and application share one event loop; it is a scheduling comparison, not a capacity figure.

    python scripts/evaluate_noisy_neighbor.py --duration 20
    -> evaluation/reports/noisy_neighbor.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time
from contextlib import ExitStack
from unittest.mock import patch

_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart  # noqa: E402
from sqlalchemy import insert  # noqa: E402

from core import fair_queue  # noqa: E402
from core.db import directory_employees  # noqa: E402
from core.identity import Principal  # noqa: E402
from core.rate_limit import MemoryRateLimiter  # noqa: E402
from core.tenancy import TenantRegistry  # noqa: E402
from core.tenant_context import tenant_scope  # noqa: E402
from evaluation.application import application, auth  # noqa: E402
from evaluation.fake_model import scripted_model  # noqa: E402
from evaluation.reporting import percentile  # noqa: E402

REPORT = _ROOT / "evaluation/reports/noisy_neighbor.json"
A_QUOTA = (60, 60)          # A: 60 turns per 60 s for the whole tenant; it sends 10x that
FLOOD_FACTOR = 10
A_BACKLOG = 200             # approved A actions queued at the start


def slow_model(latency_s: float):
    async def reply(messages, info):
        await asyncio.sleep(latency_s)
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"intent": "vpn_issue", "confidence": 0.9, "entities": {}})])
        return ModelResponse(parts=[TextPart("VPN-691 表示认证失败：确认账号未锁定，检查系统时间和客户端版本。")])
    return scripted_model(reply)


def slow_adapter(adapter, latency_s: float):
    for name in ("preflight", "apply", "observe", "current_level"):
        original = getattr(adapter, name)

        async def slowed(*args, _original=original, **kwargs):
            await asyncio.sleep(latency_s)
            return await _original(*args, **kwargs)
        setattr(adapter, name, slowed)


def requester(tenant: str, i: int) -> Principal:
    return Principal(f"dev:{tenant}{i}", f"{tenant}{i}", "engineering", frozenset({"employee"}), str(5000 + i), tenant=tenant)


def approver(tenant: str) -> Principal:
    return Principal(f"dev:{tenant}-approver", f"{tenant}-approver", "engineering", frozenset({"employee", "approver"}), "4999", tenant=tenant)


def access(employee: str) -> dict:
    return {"tool_name": "request_access", "arguments": {"employee_id": employee, "system": "GitLab", "role": "readonly", "reason": "代码审阅"}}


async def scenario(name: str, *, duration: float, model_latency: float, action_latency: float, workers: int) -> dict:
    from workflow.worker import ExecutionWorker

    noisy, protected = name != "baseline", name == "noisy_protected"
    async with application(model=slow_model(model_latency)) as (client, state):
        store = state.workflow
        registry = TenantRegistry(store.engine, cache_ttl_s=5)
        registry.create("a", actor="eval", quotas={"agent_turn": f"{A_QUOTA[0]}/{A_QUOTA[1]}"})
        registry.create("b", actor="eval")
        for tenant in ("a", "b"):
            with tenant_scope(tenant), store.engine.begin() as db:
                db.execute(insert(directory_employees), [{"tenant": tenant, "id": str(5000 + i), "username": f"{tenant}{i}",
                                                           "department": "engineering"} for i in range(A_BACKLOG + 200)])
        for adapter in state.registry.all():
            slow_adapter(adapter, action_latency)
        with ExitStack() as stack:
            if protected:
                stack.enter_context(patch.multiple(state, tenants=registry, rate_limiter=MemoryRateLimiter({})))
            else:
                # Before ADR 0035: no tenant layer and oldest-first claiming.
                stack.enter_context(patch.object(state, "tenants", None))
                stack.enter_context(patch.object(fair_queue, "rotate",
                                                 lambda rows, after: sorted(rows, key=lambda r: (r["not_before"], r["seq"]))))
            counter = iter(range(10**6))

            def approve(tenant: str) -> str:
                i = next(counter)
                approval_id = f"{tenant}-{i}"
                pending = store.create(approval_id, request_id="r-" + approval_id, conv_id="c", principal=requester(tenant, i % 200),
                                       action_requests=[access(str(5000 + i % 200))])
                store.decide(approval_id, "approved", approver(tenant), pending["payload_hash"])
                return approval_id

            if noisy:
                for _ in range(A_BACKLOG):
                    await asyncio.to_thread(approve, "a")
            # The API's own worker plus standalone ones (python -m workflow.worker), all on the same queue.
            extra = [ExecutionWorker(store, state.registry, worker_id=f"eval-{i}", base_delay_s=0) for i in range(workers - 1)]
            for worker in extra:
                worker.start()
            kick = lambda: [w.kick() for w in (state.worker, *extra)]  # noqa: E731
            kick()
            deadline = time.monotonic() + duration
            b_turns, b_statuses, b_exec, a_statuses = [], [], [], []

            async def b_user(i):
                headers = auth(f"b-user{i}", tenant="b")
                n = 0
                while time.monotonic() < deadline:
                    started = time.monotonic()
                    response = await client.post("/chat", json={"message": "VPN 连不上，报 691", "conv_id": f"b{i}",
                                                                "client_request_id": f"b{i}-{n}"}, headers=headers)
                    b_turns.append(time.monotonic() - started)
                    b_statuses.append(response.status_code)
                    n += 1
                    await asyncio.sleep(0.5)

            async def b_approvals():
                while time.monotonic() < deadline:
                    approval_id = await asyncio.to_thread(approve, "b")
                    kick()
                    started = time.monotonic()
                    while time.monotonic() - started < 60:
                        if (await asyncio.to_thread(store.get, approval_id))["execution"] in {"succeeded", "failed", "manual_review"}:
                            break
                        await asyncio.sleep(0.02)
                    b_exec.append(time.monotonic() - started)
                    await asyncio.sleep(1)

            async def a_flood():
                rate = A_QUOTA[0] / A_QUOTA[1] * FLOOD_FACTOR
                inflight, n = set(), 0
                while time.monotonic() < deadline:
                    async def one(n=n):
                        response = await client.post("/chat", json={"message": "VPN 连不上", "conv_id": f"a{n}", "client_request_id": f"a{n}"},
                                                     headers=auth(f"a-user{n % 50}", tenant="a"))
                        a_statuses.append(response.status_code)
                    task = asyncio.create_task(one())
                    inflight.add(task)
                    task.add_done_callback(inflight.discard)
                    if n % 5 == 0:
                        await asyncio.to_thread(approve, "a")   # A keeps adding to its execution backlog
                        kick()
                    n += 1
                    await asyncio.sleep(1 / rate)
                await asyncio.gather(*inflight, return_exceptions=True)

            await asyncio.gather(b_user(0), b_user(1), b_approvals(), *([a_flood()] if noisy else []))
            for worker in extra:
                await worker.stop()
    return {
        "scenario": name,
        "b_turns": len(b_turns),
        "b_turn_p50_s": round(percentile(b_turns, .5), 3),
        "b_turn_p95_s": round(percentile(b_turns, .95), 3),
        "b_429": sum(s == 429 for s in b_statuses),
        "b_errors": sum(s >= 500 for s in b_statuses),
        "b_approvals": len(b_exec),
        "b_execution_p95_s": round(percentile(b_exec, .95), 3),
        "a_requests": len(a_statuses),
        "a_429": sum(s == 429 for s in a_statuses),
    }


async def main(args) -> dict:
    results = {}
    for name in ("baseline", "noisy_protected", "noisy_unprotected"):
        results[name] = await scenario(name, duration=args.duration, model_latency=args.model_latency,
                                       action_latency=args.action_latency, workers=args.workers)
        print(json.dumps(results[name], ensure_ascii=False))
    base, guarded = results["baseline"], results["noisy_protected"]
    checks = {
        "b_turn_p95_growth_le_20pct": guarded["b_turn_p95_s"] <= base["b_turn_p95_s"] * 1.2,
        "b_no_429": guarded["b_429"] == 0,
        "b_execution_p95_le_1_5x": guarded["b_execution_p95_s"] <= base["b_execution_p95_s"] * 1.5,
    }
    return {"config": {"duration_s": args.duration, "model_latency_s": args.model_latency, "action_latency_s": args.action_latency,
                       "a_quota": f"{A_QUOTA[0]}/{A_QUOTA[1]}", "flood_factor": FLOOD_FACTOR, "a_backlog": A_BACKLOG,
                       "workers": args.workers},
            "results": results, "acceptance": checks, "passed": all(checks.values())}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--model-latency", type=float, default=0.3)
    parser.add_argument("--action-latency", type=float, default=0.02)
    parser.add_argument("--workers", type=int, default=2, help="execution workers on the queue (API worker included)")
    parser.add_argument("--output", default=str(REPORT))
    report = asyncio.run(main(parser.parse_args()))
    pathlib.Path(report_path := parser.parse_args().output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report["acceptance"]), "passed" if report["passed"] else "FAILED", "->", report_path)
    raise SystemExit(0 if report["passed"] else 1)
