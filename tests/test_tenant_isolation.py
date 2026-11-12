"""Cross-tenant sweep over every HTTP route (ADR 0022).

Tenant ``default`` owns a room, a room approval, a private approval, knowledge documents, a token
revocation and admin-audit entries, all carrying ``MARK``. Four people of tenant ``intruder`` - an
IT admin, an auditor, an approver and an employee with *the same subject as the owner* - call every
route against those objects. A route passes when the answer is 403/404 (object routes) or carries
no ``MARK`` (list routes), and the owner's data is unchanged afterwards.

Every (method, path) in the OpenAPI schema must be classified below; a new route fails
``test_every_route_is_classified`` until someone decides how it is isolated. Known leaks are listed
in ``GAPS`` with the plan phase that closes them (docs/multi-tenant-plan.md) and run as strict
xfails: they fail loudly once fixed, so the entry moves to ``PROBES``.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

import api.main as api
from api import state
from core import admin_audit
from core.db import approval_decisions, approvals
from core.retention import RetentionPolicy, RetentionSweeper
from core.session_revocation import SessionRevocations
from tests.conftest import auth
from tests.test_shared_tasks import env  # noqa: F401  (fixture)
from tests.test_task_execution import caps  # noqa: F401  (fixture)
from tests.test_workflow import ALICE, ERIN, access
from tooling.knowledge_base import KNOWLEDGE_SEARCH_SCHEMA, KnowledgeBase
from tooling.tool_manager import MCPToolManager, Tool

MARK = "TENANT-A-7F3E"
INTRUDERS = ("erin", "grace", "bob", "alice")   # it_admin, auditor, approver, owner's own subject
DENIED = {403, 404}

Ids = Dict[str, str]
Request = Callable[[Ids], Tuple[str, Dict[str, Any]]]


def get(path: str, **params) -> Request:
    return lambda ids: ("GET", path.format(**ids), {"params": params})


def post(path: str, body: Optional[dict] = None, **params) -> Request:
    def build(ids):
        payload = {k: (v.format(**ids) if isinstance(v, str) else v) for k, v in (body or {}).items()}
        return "POST", path.format(**ids), {"json": payload, "params": params}
    return build


def delete(path: str) -> Request:
    return lambda ids: ("DELETE", path.format(**ids), {})


T = "/tasks/{task}"
# (method, path) -> (request against tenant A's objects, "deny" | "list")
PROBES: Dict[Tuple[str, str], Tuple[Request, str]] = {
    ("GET", "/approvals"): (get("/approvals"), "list"),
    ("GET", "/approvals/{approval_id}"): (get("/approvals/{private}"), "deny"),
    ("POST", "/approvals/{approval_id}"): (post("/approvals/{private}", {"decision": "approved", "payload_hash": "{private_hash}"}), "deny"),
    ("POST", "/approvals/{approval_id}/confirm"): (post("/approvals/{private}/confirm", {"payload_hash": "{private_hash}"}), "deny"),
    ("POST", "/approvals/{approval_id}/withdraw"): (post("/approvals/{private}/withdraw", {}), "deny"),
    ("POST", "/approvals/{approval_id}/release"): (post("/approvals/{private}/release"), "deny"),
    ("POST", "/approvals/{approval_id}/reconcile"): (post("/approvals/{private}/reconcile"), "deny"),
    ("GET", "/approvals/{approval_id}/audit"): (get("/approvals/{private}/audit"), "deny"),
    ("GET", "/admin/audit"): (get("/admin/audit", limit=500), "list"),
    ("GET", "/admin/sessions/revocations"): (get("/admin/sessions/revocations"), "list"),
    ("GET", "/tasks"): (get("/tasks"), "list"),
    ("GET", "/task-inbox"): (get("/task-inbox"), "list"),
    ("GET", "/task-history"): (get("/task-history", q=MARK), "list"),
    ("GET", "/task-history/{task_id}"): (get("/task-history/{task}"), "deny"),
    ("POST", "/task-history/{task_id}/continue"): (post("/task-history/{task}/continue", {"message": "继续", "client_request_id": "x"}), "deny"),
    ("GET", "/tasks/{task_id}"): (get(T), "deny"),
    ("POST", "/tasks/{task_id}/messages"): (post(T + "/messages", {"message": "hi", "client_request_id": "x"}), "deny"),
    ("POST", "/tasks/{task_id}/stop"): (post(T + "/stop"), "deny"),
    ("POST", "/tasks/{task_id}/runs/{run_id}/retry"): (post(T + "/runs/r1/retry"), "deny"),
    ("POST", "/tasks/{task_id}/approvals/{approval_id}"): (post(T + "/approvals/{room_approval}", {"decision": "approved", "payload_hash": "{room_hash}"}), "deny"),
    ("GET", "/tasks/{task_id}/workspace"): (get(T + "/workspace"), "deny"),
    ("POST", "/tasks/{task_id}/progress"): (post(T + "/progress", {"version": 0, "goal": "g", "steps": []}), "deny"),
    ("POST", "/tasks/{task_id}/lifecycle"): (post(T + "/lifecycle", {"status": "resolved"}), "deny"),
    ("POST", "/tasks/{task_id}/todos"): (post(T + "/todos", {"question": "q", "assignee": "dev:bob", "client_request_id": "x"}), "deny"),
    ("POST", "/tasks/{task_id}/todos/{todo_id}/answer"): (post(T + "/todos/t1/answer", {"answer": "a"}), "deny"),
    ("POST", "/tasks/{task_id}/read"): (post(T + "/read", {"seq": 1}), "deny"),
    ("POST", "/tasks/{task_id}/reminders"): (post(T + "/reminders", {"text": "r", "due_at": 4102444800, "client_request_id": "x"}), "deny"),
    ("POST", "/tasks/{task_id}/reminders/{timer_id}/cancel"): (post(T + "/reminders/t1/cancel"), "deny"),
    ("POST", "/tasks/{task_id}/budget"): (post(T + "/budget", {"token_limit": 1, "run_limit": 1}), "deny"),
    ("POST", "/tasks/{task_id}/todos/{todo_id}/change"): (post(T + "/todos/t1/change", {"action": "cancel", "reason": "r", "client_request_id": "x"}), "deny"),
    ("GET", "/tasks/{task_id}/directory"): (get(T + "/directory"), "deny"),
    ("POST", "/tasks/{task_id}/members"): (post(T + "/members", {"subject": "dev:alice", "role": "collaborator"}), "deny"),
    ("POST", "/tasks/{task_id}/reviewers"): (post(T + "/reviewers", {"subjects": ["dev:bob"]}), "deny"),
    ("POST", "/tasks/{task_id}/archive"): (post(T + "/archive", {"archived": True}), "deny"),
    ("GET", "/tasks/{task_id}/events"): (get(T + "/events"), "deny"),
    ("POST", "/tasks/{task_id}/files"): (lambda ids: ("POST", f"/tasks/{ids['task']}/files", {"params": {"name": "a.txt", "client_request_id": "x"}, "content": b"x"}), "deny"),
    ("GET", "/tasks/{task_id}/files/{file_id}"): (get(T + "/files/f1"), "deny"),
    ("POST", "/tasks/{task_id}/suggestions/{suggestion_id}"): (post(T + "/suggestions/s1", {"accept": True}), "deny"),
    ("POST", "/tasks/{task_id}/notifications"): (post(T + "/notifications", {"channel": "email", "enabled": True}), "deny"),
    ("POST", "/tasks/{task_id}/goal"): (post(T + "/goal", {"version": 0, "checks": []}), "deny"),
    ("POST", "/tasks/{task_id}/verify"): (post(T + "/verify", {"client_request_id": "x"}), "deny"),
    ("GET", "/tasks/{task_id}/links"): (get(T + "/links"), "deny"),
    ("POST", "/tasks/{task_id}/links"): (post(T + "/links", {"target_id": "{other_task}"}), "deny"),
}

# Known cross-tenant leaks: route -> (probe, kind, plan phase that closes it).
GAPS: Dict[Tuple[str, str], Tuple[Request, str, str]] = {
    ("GET", "/knowledge/documents"): (get("/knowledge/documents"), "list", "阶段 2：知识库只按部门过滤"),
    ("DELETE", "/knowledge/documents/{document_id}"): (delete("/knowledge/documents/{doc}"), "deny", "阶段 2：别的租户的管理员能删本租户文档"),
    ("POST", "/search"): (post("/search", query=MARK), "list", "阶段 2：检索按部门，'*' 对所有租户可见"),
    ("GET", "/business/employees/{employee_id}"): (get("/business/employees/1001"), "deny", "阶段 1：沙箱目录与授权台账没有 tenant"),
}

# Act on the caller's own tenant only; ``test_own_scope_writes_stay_in_the_callers_tenant`` checks
# the owner's data afterwards, and the routes' own tests cover the rest.
OWN_SCOPE = {
    ("POST", "/tasks"), ("GET", "/auth/me"), ("POST", "/auth/logout-all"), ("POST", "/admin/sessions/revoke"),
}

# Isolation tested elsewhere with a same-subject, other-tenant caller.
COVERED = {
    ("POST", "/chat"): "test_isolation_and_history::test_private_working_and_cross_conversation_memory_are_scoped",
    ("POST", "/chat/stream"): "test_isolation_and_history::test_same_subject_other_tenant_cannot_resume_stream_or_read_trace",
    ("GET", "/chat/stream/{request_id}"): "test_isolation_and_history::test_same_subject_other_tenant_cannot_resume_stream_or_read_trace",
    ("GET", "/trace/{request_id}"): "test_isolation_and_history::test_same_subject_other_tenant_cannot_resume_stream_or_read_trace",
    ("GET", "/trace/tool/{request_id}"): "ops._owns_trace (same check as /trace/{request_id})",
    ("GET", "/trace/tools"): "ops._owns_trace filters every row by tenant",
}

# Platform-wide by design today: no tenant data, or deployment-wide state. The note says which
# plan phase makes them per tenant.
PLATFORM = {
    ("GET", "/health"): "", ("GET", "/auth/config"): "", ("POST", "/auth/dev-token"): "dev mode only",
    ("POST", "/channels/feishu/events"): "signed by Feishu; tenant mapping in 阶段 1",
    ("GET", "/metrics"): "阶段 5：内网暴露、按租户打标签", ("GET", "/monitor"): "阶段 5：全局计数",
    ("GET", "/skills"): "", ("POST", "/skills/reload"): "阶段 3：应改为平台管理员",
    ("GET", "/policy"): "阶段 3：按租户策略", ("POST", "/policy/revalidate"): "阶段 3：只复核本租户",
    ("POST", "/ops/retention/run"): "阶段 1：按租户清理", ("GET", "/knowledge/stats"): "阶段 2：无鉴权的全局片段数",
    ("POST", "/knowledge/add"): "阶段 2：写入全局知识库", ("POST", "/knowledge/upload"): "阶段 2：写入全局知识库",
}


@pytest.fixture
def world(env, caps, db_url, monkeypatch):  # noqa: F811
    tasks, workflow, _ = env
    monkeypatch.setenv("IT_COPILOT_SEED_DEMO_DOCS", "false")
    kb = KnowledgeBase(db_url)
    tools = MCPToolManager("sk-local")

    async def knowledge(params, ctx=None):
        return await kb.search_handler(params, ctx)
    tools.register(Tool(name="knowledge_search", description="kb", handler=knowledge, schema=KNOWLEDGE_SEARCH_SCHEMA))
    sessions = SessionRevocations(workflow.engine, cache_ttl_s=0)
    for name, value in {"workflow": workflow, "tasks": tasks, "task_capabilities": caps, "task_worker": None, "worker": None,
                        "kb": kb, "tool_manager": tools, "sessions": sessions, "memory": None, "tenants": None,
                        "retention": RetentionSweeper(workflow.engine, RetentionPolicy())}.items():
        monkeypatch.setattr(state, name, value)

    room = tasks.create(ALICE, f"{MARK} GitLab 权限", f"{MARK} 申请 GitLab 只读权限", "tenant-a")
    tasks.finish(tasks.claim(), {"response": "请审批", "proposal": {"action_requests": [access(reason=MARK)]}})
    draft = tasks.get(room["id"], ALICE)["approvals"][0]
    other = tasks.create(ALICE, f"{MARK} 第二个事项", f"{MARK} 关联目标", "tenant-a-2")
    workflow.confirm(draft["approval_id"], ALICE, draft["payload_hash"])
    private = workflow.create("ap-tenant-a", request_id="r-tenant-a", conv_id="conv-tenant-a", principal=ALICE,
                              action_requests=[access(role="reporter", reason=MARK)])
    kb.add_documents([{"title": f"{MARK} VPN SOP", "content": f"{MARK} VPN 证书排障"},
                      {"title": f"{MARK} 研发 SOP", "content": f"{MARK} 研发专用", "department": "engineering"}])
    sessions.revoke("default", f"dev:{MARK}", actor=ERIN.subject, reason=MARK)
    admin_audit.record(workflow.engine, ERIN, "knowledge_added", MARK, {"titles": [MARK]})
    ids = {"task": room["id"], "other_task": other["id"], "room_approval": draft["approval_id"], "room_hash": draft["payload_hash"],
           "private": private["approval_id"], "private_hash": private["payload_hash"],
           "doc": next(d["document_id"] for d in kb.list_documents() if d["title"] == f"{MARK} VPN SOP")}
    return TestClient(api.app), ids, workflow, tasks, kb, sessions


def approval_rows(workflow):
    with workflow.engine.connect() as db:
        rows = db.execute(select(approvals.c.id, approvals.c.status, approvals.c.execution, approvals.c.version)).all()
        votes = db.execute(select(func.count()).select_from(approval_decisions)).scalar()
    return sorted(map(tuple, rows)), votes


def snapshot(workflow, tasks, kb, sessions, ids):
    """What tenant A owns, as tenant A sees it."""
    room = tasks.get(ids["task"], ALICE)
    return {
        "approvals": approval_rows(workflow),
        "room": (room["status"], len(room["events"]), room.get("archived")),
        "documents": sorted(d["document_id"] for d in kb.list_documents()),
        "revocations": [r["subject"] for r in sessions.list("default")],
    }


def probe(client, request: Request, kind: str, ids: Ids) -> list:
    """Run one probe as every intruder; return the failures."""
    failures = []
    for who in INTRUDERS:
        method, url, kwargs = request(ids)
        response = client.request(method, url, headers=auth(who, tenant="intruder"), **kwargs)
        leaked = MARK in response.text
        if response.status_code == 422:
            failures.append(f"{who}: {method} {url} -> 422 (probe body is wrong, so it proves nothing): {response.text[:200]}")
        elif kind == "deny" and response.status_code not in DENIED:
            failures.append(f"{who}: {method} {url} -> {response.status_code} {response.text[:200]}")
        elif leaked:
            failures.append(f"{who}: {method} {url} -> {response.status_code} leaked {MARK}")
    return failures


def routes():
    return {(method.upper(), path) for path, ops in api.app.openapi()["paths"].items() for method in ops}


def test_every_route_is_classified():
    groups = [set(PROBES), set(GAPS), OWN_SCOPE, set(COVERED), set(PLATFORM)]
    classified = set().union(*groups)
    assert sum(map(len, groups)) == len(classified), "a route is classified twice"
    assert routes() - classified == set(), "new route: decide how it is tenant-isolated and classify it here"
    assert classified - routes() == set(), "classified route no longer exists"


def test_no_route_reaches_another_tenants_objects(world):
    client, ids, workflow, tasks, kb, sessions = world
    before = snapshot(workflow, tasks, kb, sessions, ids)
    assert dict((r[0], r[1]) for r in before["approvals"][0])[ids["private"]] == "pending" and len(before["documents"]) == 2 and before["revocations"] == [f"dev:{MARK}"]
    for who in ("alice", "erin"):  # the owner's tenant sees the marked data, so the sweep would notice a leak
        assert MARK in client.get("/tasks", headers=auth(who)).text
    failures = [f for request, kind in PROBES.values() for f in probe(client, request, kind, ids)]
    assert failures == [], "\n".join(failures)
    assert snapshot(workflow, tasks, kb, sessions, ids) == before


@pytest.mark.parametrize("route", sorted(GAPS), ids=lambda r: f"{r[0]} {r[1]}")
def test_known_gap(world, route):
    request, kind, phase = GAPS[route]
    client, ids, *_ = world
    failures = probe(client, request, kind, ids)
    if failures:
        pytest.xfail(f"{phase}: " + failures[0])
    pytest.fail(f"{route} no longer leaks: move it from GAPS to PROBES")


def test_own_scope_writes_stay_in_the_callers_tenant(world):
    client, ids, workflow, tasks, kb, sessions = world
    before = snapshot(workflow, tasks, kb, sessions, ids)
    erin, alice = auth("erin", tenant="intruder"), auth("alice", tenant="intruder")
    created = client.post("/tasks", headers=alice, json={"title": "B 的事项", "message": "B", "client_request_id": "tenant-a"})
    assert created.status_code == 201, created.text
    assert created.json()["id"] != ids["task"]                      # same subject and client id, different tenant
    assert client.get("/auth/me", headers=alice).json()["tenant"] == "intruder"
    assert client.post("/admin/sessions/revoke", headers=erin, json={"subject": f"dev:{MARK}", "reason": "b"}).status_code == 200
    assert client.post("/auth/logout-all", headers=alice).status_code == 200
    after = snapshot(workflow, tasks, kb, sessions, ids)
    assert after == before
    assert sorted(r["subject"] for r in sessions.list("intruder")) == sorted(["dev:alice", f"dev:{MARK}"])
    assert client.get("/tasks", headers=auth("alice")).status_code == 200   # A's alice was not signed out
