"""Operator decisions, independent worker traces and metric series contracts."""
import asyncio
import io
import json
import logging

import httpx2
import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select

from it_copilot.infra.db import actions, approvals, admin_audit
from it_copilot.infra.logging import WorkflowFormatter
from it_copilot.workflow.adapters import AdapterRegistry
from it_copilot.workflow.adapters.base import TransientError
from it_copilot.workflow.worker import ExecutionWorker
from tests.support.application import application, auth
from tests.test_application_execution import access_model
from tests.test_observability import sample
from tests.test_tool_executor_and_adapters import gitea
from tests.test_workflow import BOB, create


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution,expected", [
    ("retry", "unknown"), ("accept_external", "succeeded"), ("mark_resolved", "cancelled"),
])
async def test_operator_resolution(resolution, expected, monkeypatch):
    async with application(embed_worker=False) as (http, state):
        store = state.workflow
        draft = create(store)
        store.decide("a", "approved", BOB, draft["payload_hash"])
        action, = store.claim("interrupted", 60)
        store.save_result(action, {"baseline_level": None})
        store.fail(action, "operator review", status="manual_review")
        step, = store.get("a")["results"]
        path = f"/approvals/a/actions/{action['id']}/resolve"
        payload = {"resolution": resolution, "version": step["version"], "reason": "工单核实", "resolution_note": "线下处理"}
        post = lambda **changes: http.post(path, headers=auth("erin"), json={**payload, **changes})
        assert (await http.post(path, json=payload)).status_code == 403
        assert (await post(reason=" ")).status_code == 422
        if resolution == "accept_external":
            async def unavailable(*args):
                raise TransientError("unavailable")
            with monkeypatch.context() as patch:
                patch.setattr(state.registry.local, "observe", unavailable)
                assert (await post()).status_code == 409
            assert store.get("a")["results"][0]["version"] == step["version"]
            from it_copilot.infra.tenant_context import tenant_scope
            with tenant_scope("default"):
                await state.registry.local.apply(action["id"], action)
        metric = "it_copilot_workflow_manual_resolutions_total"
        before = sample(metric, resolution=resolution)
        responses = await asyncio.gather(post(), post())
        assert sorted(r.status_code for r in responses) == [200, 409]
        body = next(r.json() for r in responses if r.status_code == 200)
        assert body["results"][0]["status"] == expected
        assert body["audit"][-1]["detail"]["reason"] == "工单核实"
        with store.engine.connect() as db:
            detail = db.execute(select(admin_audit.c.detail).where(admin_audit.c.subject == action["id"])).scalar_one()
        assert detail["version"] == step["version"] and detail["resolution"] == resolution
        assert sample(metric, resolution=resolution) == before + 1
        if resolution == "accept_external":
            assert body["execution"] == "succeeded" and len(body["revocations"]) == 1
            assert store.employee("1001", "default")["access_ledger"][0]["status"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("incoming", [None, "00-" + "a"*32 + "-" + "b"*16 + "-01"])
async def test_chat_outbox_gitea_and_expiry_trace(incoming, monkeypatch):
    requests = []
    level = None

    def target(request):
        nonlocal level
        requests.append((request.method, request.headers["traceparent"].split("-")[1]))
        if request.method == "PUT":
            level = json.loads(request.content)["permission"]
        elif request.method == "DELETE":
            level = None
        elif request.url.path.endswith("/permission"):
            return httpx2.Response(200, json={"permission": level})
        elif request.url.path.endswith("/collaborators/alice"):
            return httpx2.Response(204 if level else 404)
        return httpx2.Response(204)

    async with application(model=access_model(), embed_worker=False) as (http, state):
        monkeypatch.setenv("IT_COPILOT_APPROVAL_WAIT_S", "0")
        adapter = gitea(target)
        store = state.workflow
        registry = AdapterRegistry(state.registry.local, {"gitlab": adapter})
        store.route = lambda spec: registry.for_action(spec).name
        try:
            response = await http.post("/chat", json={"message": "申请 GitLab 只读权限，工号1001，理由代码审阅"},
                                       headers={"traceparent": incoming} if incoming else {})
            trace_id = response.headers["X-Trace-Id"]
            assert len(trace_id) == 32 and int(trace_id, 16)
            assert trace_id == response.headers["traceparent"].split("-")[1] == response.json()["trace_id"]
            if incoming:
                assert trace_id == incoming.split("-")[1]
            key = response.json()["approval_id"]
            path = "/approvals/" + key
            digest = (await http.get(path)).json()["payload_hash"]
            await http.post(path + "/confirm", json={"payload_hash": digest})
            await http.post(path, headers=auth("bob"), json={"payload_hash": digest, "decision": "approved"})
            worker = ExecutionWorker(store, registry)
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            handler.setFormatter(WorkflowFormatter("api", True))
            logger = logging.getLogger("it_copilot.workflow.worker")
            logger.addHandler(handler)
            try:
                await worker.run_until_idle()
                with store.engine.connect() as db:
                    parent = db.scalar(select(approvals.c.traceparent).where(approvals.c.id == key))
                    jobs = db.execute(select(actions.c.traceparent, actions.c.not_before).where(actions.c.approval_id == key)).all()
                assert parent.split("-")[1] == trace_id and len(jobs) == 2
                assert all(j.traceparent.split("-")[1] == trace_id for j in jobs)
                store.clock = lambda: max(j.not_before for j in jobs) + 1
                await worker.run_until_idle()
            finally:
                logger.removeHandler(handler)
            assert {method for method, _ in requests} >= {"GET", "PUT", "DELETE"}
            assert {t for _, t in requests} == {trace_id}
            logs = [json.loads(line) for line in stream.getvalue().splitlines()]
            effects = [entry for entry in logs if entry.get("approval_id") == key]
            assert len({entry["action_id"] for entry in effects}) == 2
            assert all((entry["component"], entry["trace_id"], entry["tenant"]) == ("worker", trace_id, "default") for entry in effects)
        finally:
            await adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution,status", [("mark_resolved", "resolved"), ("accept_external", "revoked")])
async def test_review_drifted_grant_after_automatic_revoke_was_cancelled(resolution, status):
    from it_copilot.infra.tenant_context import tenant_scope
    from it_copilot.workflow.reconciler import GrantReconciler

    async with application(embed_worker=False) as (http, state):
        store = state.workflow
        draft = create(store)
        store.decide("a", "approved", BOB, draft["payload_hash"])
        await ExecutionWorker(store, state.registry).run_until_idle()
        with tenant_scope("default"):
            await state.registry.local.apply("offline-removal", {"tool_name": "revoke_access",
                "arguments": {"employee_id": "1001", "system": "gitlab", "restore_to": None}})
        assert await GrantReconciler(store, state.registry).run_once() == 1
        step, = store.get("a")["revocations"]
        assert step["drifted"] and step["status"] == "cancelled"
        response = await http.post(f"/approvals/a/actions/{step['operation_id']}/resolve", headers=auth("erin"),
                                   json={"resolution": resolution, "version": step["version"], "reason": "核实线下撤回"})
        assert response.status_code == 200 and not response.json()["revocations"][0]["drifted"]
        assert store.employee("1001", "default")["access_ledger"][0]["status"] == status


def test_registry_cardinality():
    forbidden = {"user_id", "tenant", "approval_id", "action_id", "conv_id", "request_id"}
    for collector in REGISTRY._collector_to_names:
        for family in collector.describe() if hasattr(collector, "describe") else []:
            if family.name.startswith("it_copilot_"):
                assert not family.name.startswith(("it_copilot_channel_messages", "it_copilot_channel_deliveries"))
                assert forbidden.isdisjoint(collector._labelnames), family.name


@pytest.mark.asyncio
async def test_queue_measurements(db_url):
    from it_copilot.workflow.store import WorkflowStore
    store = WorkflowStore(db_url)
    try:
        notifier = store.notifier
        for source in ("notify", "kick", "poll"):
            metric = "it_copilot_workflow_wakeups_total"
            before = sample(metric, source=source)
            notifier.event.clear()
            if source != "poll":
                notifier.signal(source)
            await notifier.wait(.001)
            assert sample(metric, source=source) == before + 1
        draft = create(store)
        store.decide("a", "approved", BOB, draft["payload_hash"])
        for reason in ("shutdown", "error"):
            before = sample("it_copilot_workflow_dispatch_latency_seconds_count")
            action, = await store.actions.claim("test", 60)
            assert sample("it_copilot_workflow_dispatch_latency_seconds_count") == before + 1
            before = sample("it_copilot_workflow_leases_released_total", reason=reason)
            await store.actions.release_lease(action, reason=reason)
            assert sample("it_copilot_workflow_leases_released_total", reason=reason) == before + 1
    finally:
        await store.aclose()
