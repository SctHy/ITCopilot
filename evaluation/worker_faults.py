"""SIGKILL trials at durable target effects, using logical lease time and real workers."""
import asyncio
import json
import os
from pathlib import Path
import signal
import sys

from sqlalchemy import select

from core.db import access_grants, read
from core.identity import Principal
from core.tenant_context import system_scope, tenant_scope
from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from workflow.store import WorkflowStore
from workflow.worker import ExecutionWorker

T0 = 1_800_000_000.0
REQUESTER = Principal("eval:applicant", "applicant", "it", frozenset({"employee", "it_admin"}), "1001")
APPROVERS = [Principal("eval:bob", "bob", "it", frozenset({"approver"}), "2001"),
             Principal("eval:carol", "carol", "it", frozenset({"approver"}), "2002")]


class FaultAdapter(LocalDirectoryAdapter):
    def __init__(self, engine, calls_path, fault_action=None):
        super().__init__(engine)
        self.calls_path, self.fault_action = Path(calls_path), fault_action

    async def apply(self, action_id, action):
        with self.calls_path.open("a") as stream:
            stream.write(json.dumps({"action_id": action_id, "tool": action["tool_name"]}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        result = await super().apply(action_id, action)
        if action_id == self.fault_action:
            print("WORKER_FAULT_POINT", flush=True)
            await asyncio.Event().wait()
        return result


def access(employee="1001", system="gitlab", role="reporter", hours=24):
    return {"tool_name": "request_access", "arguments": {
        "employee_id": employee, "system": system, "role": role, "duration_hours": hours, "reason": "故障评测"}}


def approve(store, name, items):
    body = store.create(name, request_id=name, conv_id=name, principal=REQUESTER, action_requests=items)
    for principal in APPROVERS:
        body = store.decide(name, "approved", principal, body["payload_hash"])
    return body


async def worker_child(url, calls_path, scenario, phase, fault_action):
    due = T0 + 86401 if scenario == "worker_revoke" else T0
    now = due if phase == "crash" else due + 6
    with system_scope():
        store = WorkflowStore(url, clock=lambda: now)
        adapter = FaultAdapter(store.engine, calls_path, fault_action if phase == "crash" else None)
        worker = ExecutionWorker(store, AdapterRegistry(adapter), worker_id="fault-" + phase, lease_s=5)
        try:
            await worker.run_until_idle()
        finally:
            store.close()


async def worker_trial(directory, scenario):
    url = "sqlite:///" + str(directory / "worker.sqlite3")
    calls_path = directory / "apply_calls.jsonl"
    with system_scope():
        store = WorkflowStore(url, clock=lambda: T0)
        adapter = FaultAdapter(store.engine, calls_path)
        if scenario == "worker_apply":
            body = approve(store, "a", [access()])
            fault_action = body["results"][0]["operation_id"]
        elif scenario == "worker_batch_second":
            body = approve(store, "a", [access(), access("1002", "jira", "readonly")])
            fault_action = body["results"][1]["operation_id"]
        elif scenario == "worker_revoke":
            approve(store, "a", [access()])
            worker = ExecutionWorker(store, AdapterRegistry(adapter))
            await worker.run_until_idle()
            approve(store, "b", [access(role="developer", hours=72)])
            await worker.run_until_idle()
            fault_action = store.get("a")["revocations"][0]["operation_id"]
        else:
            raise ValueError(scenario)
        store.close()
    script = Path(__file__).resolve().parents[1] / "scripts/evaluate_long_tasks.py"
    command = [sys.executable, str(script), "--database", url, "--calls-path", str(calls_path),
               "--worker-scenario", scenario, "--fault-action", fault_action]
    process = await asyncio.create_subprocess_exec(*command, "--child", "worker-crash",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        marker = await asyncio.wait_for(process.stdout.readline(), 30)
        if marker.strip() != b"WORKER_FAULT_POINT":
            raise RuntimeError("worker did not reach fault point: " + (await process.stderr.read()).decode()[-2000:])
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()
    killed = process.returncode == -signal.SIGKILL
    with system_scope():
        interrupted = WorkflowStore(url, clock=lambda: T0)
        during = interrupted.get("a")
        held = [row for row in during["results"] + during["revocations"] if row["operation_id"] == fault_action][0]
        interrupted.close()
    resumed = await asyncio.create_subprocess_exec(*command, "--child", "worker-resume",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, errors = await asyncio.wait_for(resumed.communicate(), 30)
    except BaseException:
        if resumed.returncode is None:
            resumed.kill()
        await resumed.communicate()
        raise
    if resumed.returncode:
        raise RuntimeError(errors.decode()[-2000:])
    with system_scope():
        recovered = WorkflowStore(url, clock=lambda: T0 + 86407 if scenario == "worker_revoke" else T0 + 6)
        try:
            body = recovered.get("a")
            target = [row for row in body["results"] + body["revocations"] if row["operation_id"] == fault_action][0]
            calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
            counts = {row["action_id"]: sum(call["action_id"] == row["action_id"] for call in calls) for row in calls}
            with tenant_scope("default"):
                current = await LocalDirectoryAdapter(recovered.engine).current_level("1001", "gitlab")
            with read(recovered.engine) as db:
                desired = recovered.desired_level(db, "default", "1001", "gitlab")
                grants = [dict(row) for row in db.execute(select(access_grants)).mappings()]
            events = recovered.audit("a")
            reconciled = target["status"] == "succeeded" and target["reconciled"]
            expected_calls = 2 if scenario == "worker_batch_second" else 3 if scenario == "worker_revoke" else 1
            passed = (killed and held["status"] == "executing" and reconciled and all(n == 1 for n in counts.values())
                      and len(calls) == expected_calls and current == desired and body["execution"] == "succeeded")
            return {"scenario": scenario, "passed": passed, "kill_signal": "SIGKILL", "child_returncode": process.returncode,
                "fault_point": "adapter.apply returned, store.succeed not called", "interrupted_status": held["status"],
                "recovered_status": target["status"], "reconciled": reconciled, "apply_calls": len(calls),
                "apply_calls_by_action": counts, "current_level": current, "desired_level": desired,
                "grant_statuses": [row["status"] for row in grants], "audit_events": [row["event"] for row in events],
                "lease_clock": {"killed_at": T0 + 86401 if scenario == "worker_revoke" else T0,
                                "restarted_after_s": 6, "lease_s": 5}}
        finally:
            recovered.close()
