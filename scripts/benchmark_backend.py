"""Real HTTP + SQL + isolated Redis mixed load; no model calls or external IAM writes."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx2
import uvicorn
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from core.identity import Principal
from core.tenant_context import tenant_scope
from evaluation.application import application, auth
from evaluation.fake_model import scripted_model
from scripts.backup import temporary_redis


def summarize(values):
    ordered = sorted(values)
    return {"count": len(ordered), **{f"p{p}_ms": round(ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)] * 1000, 3) if ordered else None for p in (50, 95, 99)}}


@contextmanager
def isolated_postgres(admin_url):
    if not admin_url:
        yield None
        return
    parsed = make_url(admin_url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("IT_COPILOT_BENCH_POSTGRES_URL must be PostgreSQL")
    name = "itc_bench_" + uuid.uuid4().hex
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    created = False
    try:
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {name}"))
        created = True
        yield parsed.set(database=name).render_as_string(hide_password=False)
    finally:
        if created:
            with admin.connect() as conn:
                conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
        admin.dispose()


async def measure(args, database_url, redis_url):
    async def reject_model(*_):
        raise AssertionError("Backend benchmark must never call a model")

    async with application(model=scripted_model(reject_model), redis_url=redis_url, database_url=database_url) as (_client, state):
        from api.main import app
        # Read task snapshots but do not benchmark Agent execution; the approval worker remains live.
        if state.task_worker:
            await state.task_worker.stop()
        principal = Principal("dev:alice", "alice", "engineering", frozenset({"employee"}), "1001")
        fixtures = []
        total = args.runs + args.warmup
        with tenant_scope("default"):
            for i in range(total):
                await asyncio.to_thread(state.tasks.create, principal, f"bench-{i}", "后端基准测试事项", f"bench-{i}")
                body = await asyncio.to_thread(state.workflow.create, f"bench-approval-{i}", request_id=f"bench-request-{i}", conv_id=f"bench-conversation-{i}", principal=principal, ask_requester=True,
                    action_requests=[{"tool_name": "request_access", "arguments": {"employee_id": "1001", "system": "GitLab", "role": "readonly", "reason": "后端基准测试"}}])
                fixtures.append((f"bench-approval-{i}", body["payload_hash"]))
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="error"))
        serving = asyncio.create_task(server.serve())
        rows, errors, sql_durations, pool_samples = {}, [], [], []
        lock = threading.Lock()
        collecting = False

        def before(conn, _cursor, _statement, _parameters, _context, _many):
            conn.info["bench_started"] = time.perf_counter()

        def after(conn, _cursor, _statement, _parameters, _context, _many):
            if collecting:
                with lock:
                    sql_durations.append(time.perf_counter() - conn.info["bench_started"])
        event.listen(state.workflow.engine, "before_cursor_execute", before)
        event.listen(state.workflow.engine, "after_cursor_execute", after)
        try:
            for _ in range(500):
                if server.started:
                    break
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
            else:
                raise RuntimeError("HTTP benchmark server failed to start")
            port = server.servers[0].sockets[0].getsockname()[1]
            async with httpx2.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=60) as client:
                async def request(name, method, path, who, payload=None, record=True):
                    started = time.perf_counter()
                    response = await client.request(method, path, headers=auth(who), json=payload)
                    if record:
                        rows.setdefault(name, []).append(time.perf_counter() - started)
                    if response.status_code >= 400:
                        errors.append({"operation": name, "status": response.status_code})
                        response.raise_for_status()
                    return response.json()

                async def operation(fixture, record):
                    approval, frozen = fixture
                    # Each cycle: two list reads, requester confirmation, independent approval.
                    await request("tasks_read", "GET", "/tasks", "alice", record=record)
                    await request("approvals_read", "GET", "/approvals", "bob", record=record)
                    await request("confirm", "POST", f"/approvals/{approval}/confirm", "alice", {"payload_hash": frozen}, record)
                    await request("approve", "POST", f"/approvals/{approval}", "bob", {"decision": "approved", "payload_hash": frozen}, record)

                for fixture in fixtures[:args.warmup]:
                    await operation(fixture, False)
                sem = asyncio.Semaphore(args.concurrency)

                async def bounded(fixture):
                    async with sem:
                        await operation(fixture, True)
                        checkedout = getattr(state.workflow.engine.pool, "checkedout", None)
                        if checkedout:
                            pool_samples.append(checkedout())

                started = time.perf_counter()
                collecting = True
                await asyncio.gather(*(bounded(fixture) for fixture in fixtures[args.warmup:]))
                elapsed = time.perf_counter() - started
                collecting = False
                # HTTP 200 alone does not establish successful execution.
                execution = []
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    with tenant_scope("default"):
                        execution = [await asyncio.to_thread(state.workflow.get, item[0]) for item in fixtures]
                    if all(item["execution"] == "succeeded" for item in execution):
                        break
                    await asyncio.sleep(0.1)
                metrics = (await client.get("/metrics")).text
                actions_ok = all(item["execution"] == "succeeded" for item in execution)
                with tenant_scope("default"):
                    audits = [await asyncio.to_thread(state.workflow.audit, item[0]) for item in fixtures]
                exactly_one_decision = all(sum(row["event"] == "approved" for row in audit) == 1 for audit in audits)
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.with_suffix(".prom").write_text(metrics)
                return {"database": "postgresql" if database_url else "sqlite", "redis": "isolated real Redis", "transport": "loopback HTTP with connection reuse", "model_calls": 0,
                        "load": "closed-loop bounded concurrency; one employee/resource hotspot", "runs": args.runs, "warmup_runs": args.warmup, "concurrency": args.concurrency,
                        "elapsed_seconds": round(elapsed, 3), "http_requests": sum(map(len, rows.values())), "requests_per_second": round(sum(map(len, rows.values())) / elapsed, 2),
                        "operations": {key: summarize(value) for key, value in rows.items()}, "sql_statement_latency": summarize(sql_durations), "pool_checked_out_max": max(pool_samples, default=None),
                        "errors": errors, "all_actions_succeeded": actions_ok, "one_approval_audit_each": exactly_one_decision,
                        "passed": not errors and actions_ok and exactly_one_decision,
                        "limits": "No model quality or external IAM measurement; fixture creation excluded; small dataset, single run, closed-loop load; not production capacity."}
        finally:
            collecting = False
            event.remove(state.workflow.engine, "before_cursor_execute", before)
            event.remove(state.workflow.engine, "after_cursor_execute", after)
            server.should_exit = True
            await serving


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=40)
    parser.add_argument("--warmup", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", type=Path, default=ROOT / "output/backend-benchmark.json")
    args = parser.parse_args()
    if not 1 <= args.runs <= 1000 or not 0 <= args.warmup <= 100 or not 1 <= args.concurrency <= 32:
        parser.error("runs 1..1000, warmup 0..100, concurrency 1..32")
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["IT_COPILOT_MASK_MODEL"] = "off"
    with isolated_postgres(os.getenv("IT_COPILOT_BENCH_POSTGRES_URL")) as database_url, temporary_redis() as redis:
        from urllib.parse import quote
        socket_path = redis.connection_pool.connection_kwargs["path"]
        result = asyncio.run(measure(args, database_url, "unix://" + quote(socket_path, safe="/")))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "passed": result["passed"], "http_requests": result["http_requests"]}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
