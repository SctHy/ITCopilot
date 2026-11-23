"""Repeatable acceptance workloads for the workflow and event-stream protocols."""

import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager
import json
import math
from pathlib import Path
import tempfile
import time
import uuid

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url

from it_copilot.infra.db import actions, read
from it_copilot.infra.identity import Principal
from it_copilot.workflow.adapters import AdapterRegistry
from it_copilot.workflow.store import WorkflowStore
from it_copilot.workflow.worker import ExecutionWorker


def percentiles(samples, *ranks):
    ordered = sorted(samples)
    return {f"p{p}": round(ordered[math.ceil(len(samples) * p / 100) - 1], 3) for p in ranks}


@contextmanager
def database(root, base):
    if not base:
        yield f"sqlite:///{root / 'workflow.db'}"
        return
    url = make_url(base)
    if url.get_backend_name() != "postgresql":
        raise ValueError("--database-url requires PostgreSQL")
    url = url.set(drivername="postgresql+psycopg")
    engine = create_engine(url)
    schema = "bench_" + uuid.uuid4().hex
    with engine.begin() as conn:
        if conn.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL")):
            raise ValueError("use a dedicated benchmark database without application migrations")
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    try:
        yield url.update_query_dict({"options": f"-csearch_path={schema},public"}).render_as_string(
            hide_password=False
        )
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()


def approve(store, name, resource, hours=1):
    employee = str(10000 + resource)
    owner = Principal(
        "bench:" + employee, employee, "engineering", frozenset({"employee"}), employee
    )
    reviewer = Principal("bench:review", "review", "engineering", frozenset({"approver"}), "2001")
    action = {
        "tool_name": "request_access",
        "arguments": {
            "employee_id": employee,
            "system": "gitlab",
            "role": "readonly",
            "reason": "bench",
            "duration_hours": hours,
        },
    }
    body = store.create(
        name, request_id=name, conv_id=name, principal=owner, action_requests=[action]
    )
    store.decide(name, "approved", reviewer, body["payload_hash"])


def desired(action):
    args = action["arguments"]
    return args.get("restore_to") if action["tool_name"] == "revoke_access" else args["role"]


class MemoryTarget:
    name = "local"

    def __init__(self):
        self.levels = {}
        self.writes = Counter()
        self.active = Counter()
        self.conflicts = 0

    def level(self, role):
        return role

    async def preflight(self, action):
        pass

    async def current_level(self, employee_id, system):
        return self.levels.get(employee_id)

    async def apply(self, action_id, action):
        key = action["arguments"]["employee_id"]
        self.writes[action_id] += 1
        self.conflicts += int(self.active[key] > 0)
        self.active[key] += 1
        try:
            await asyncio.sleep(0.005)
            self.levels[key] = desired(action)
            return {}
        finally:
            self.active[key] -= 1

    async def observe(self, action_id, action):
        return self.levels.get(action["arguments"]["employee_id"]) == desired(action)


async def concurrency(args, root):
    offset = 0
    with database(root, args.database_url) as url:
        store = WorkflowStore(url, clock=lambda: time.time() + offset)
        target = MemoryTarget()
        starts = Counter()
        ready = {}
        latency = []
        grant_started = {}
        settled = set()
        failures = []

        def event(stream, kind, data):
            if kind == "action_started":
                starts[data["action_id"]] += 1
                if data["kind"] == "apply":
                    grant_started[data["action_id"]] = store.clock()
                else:
                    latency.append((time.monotonic() - ready[stream]) * 1000)

            elif kind == "action_succeeded":
                settled.add(data["action_id"])
            else:
                failures.append((kind, data))

        async def wait_for_count(count):
            while len(settled) < count:
                if failures:
                    raise RuntimeError(f"unexpected workflow outcome: {failures[0]}")
                await asyncio.sleep(0.005)

        workers = [
            ExecutionWorker(store, AdapterRegistry(target), events=event, poll_s=0.1)
            for _ in range(args.workers)
        ]
        grants = (args.actions + 1) // 2
        try:
            for w in workers:
                w.start()
            if args.database_url:
                async with asyncio.timeout(10):
                    while any(w.notifier.source is None for w in workers):
                        await asyncio.sleep(0.01)
            begin = time.monotonic()
            first_batch = max(1, grants // 2)
            async with asyncio.timeout(max(60, args.actions * 2)):
                for i in range(grants):
                    if i == first_batch:
                        await wait_for_count(first_batch)
                        offset += 3601
                        for j in range(first_batch):
                            ready[f"approval:grant-{j}"] = time.monotonic()
                    name = f"grant-{i}"
                    hours = 3 if args.actions % 2 and i == grants - 1 else 1
                    await store.run(approve, store, name, i % args.resources, hours)
                mixed_count = grants + (first_batch if first_batch < grants else 0)
                await wait_for_count(mixed_count)
                offset += 3601
                for i in range(first_batch if first_batch < grants else 0, grants):
                    ready[f"approval:grant-{i}"] = time.monotonic()
                await wait_for_count(args.actions)
            elapsed = time.monotonic() - begin
            if args.database_url:
                assert any(w.notifier.source == "notify" for w in workers), (
                    "no PostgreSQL notification observed"
                )
            with read(store.engine) as conn:
                rows = conn.execute(select(actions.c.id, actions.c.not_before, actions.c.status))
                for action_id, due, status in rows:
                    if action_id in settled:
                        assert status == "succeeded"
                    if action_id in grant_started:
                        latency.append(1000 * (grant_started[action_id] - due))
            assert len(latency) == args.actions
            duplicates = sum(starts.values()) - len(starts)
            repeated_writes = sum(target.writes.values()) - len(target.writes)
            expected = {str(10000 + i % args.resources): None for i in range(grants)}
            if args.actions % 2:
                expected[str(10000 + (grants - 1) % args.resources)] = "readonly"
            assert target.levels == expected and len(starts) == args.actions
            assert not (duplicates or repeated_writes or target.conflicts)
            return dict(
                database=make_url(url).get_backend_name(),
                workers=args.workers,
                resources=len(expected),
                actions=len(starts),
                seconds=round(elapsed, 3),
                actions_s=round(len(starts) / elapsed, 2),
                scheduling_ms=percentiles(latency, 50, 95, 99),
                duplicate_executions=duplicates,
                duplicate_writes=repeated_writes,
                write_conflicts=target.conflicts,
                consistency_pct=100,
            )
        finally:
            await asyncio.gather(*(w.stop() for w in workers))
            await store.aclose()


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def main():
    if not __debug__:
        raise RuntimeError("bench requires Python assertions enabled")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("concurrency", "fault", "sse"):
        sub = commands.add_parser(name)
        sub.add_argument("--json", action="store_true")
        if name == "concurrency":
            for key, default in (("workers", 4), ("resources", 6), ("actions", 48)):
                sub.add_argument("--" + key, type=positive, default=default)
        if name in {"concurrency", "fault"}:
            sub.add_argument("--database-url")
        elif name == "sse":
            sub.add_argument("--clients", type=positive, default=24)
            sub.add_argument("--events-per-stream", type=positive, default=20)
            group = sub.add_mutually_exclusive_group()
            group.add_argument("--redis-url")
            group.add_argument("--in-memory", action="store_true")
    args = parser.parse_args()
    from .bench_fault import fault
    from .bench_sse import sse

    with tempfile.TemporaryDirectory() as folder:
        job = {"concurrency": concurrency, "fault": fault, "sse": sse}[args.command]
        result = asyncio.run(job(args, Path(folder)))
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))


if __name__ == "__main__":
    main()
