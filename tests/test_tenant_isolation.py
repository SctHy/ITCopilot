"""Cross-tenant sweep over every HTTP route (ADR 0022).

Tenant ``default`` owns a private approval, knowledge documents, a token
revocation and admin-audit entries, all carrying ``MARK``. Four people of tenant ``intruder`` - an
IT admin, an auditor, an approver and an employee with *the same subject as the owner* - call every
route against those objects. A route passes when the answer is 403/404 (object routes) or carries
no ``MARK`` (list routes), and the owner's data is unchanged afterwards.

Every (method, path) in the OpenAPI schema must be classified below; a new route fails
``test_every_route_is_classified`` until someone decides how it is isolated. Known leaks are listed
in ``GAPS`` with the violated isolation contract and run as strict
xfails: they fail loudly once fixed, so the entry moves to ``PROBES``.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from tests import runtime as api
from tests.runtime import state
from it_copilot.infra import admin_audit
from it_copilot.infra.db import approval_decisions, approvals
from it_copilot.infra.retention import RetentionPolicy, RetentionSweeper
from it_copilot.infra.session_revocation import SessionRevocations
from tests.conftest import auth
from tests.test_workflow import ALICE, ERIN, access
from it_copilot.agent.tooling.knowledge_base import KNOWLEDGE_SEARCH_SCHEMA, KnowledgeBase
from it_copilot.agent.tooling.tool_manager import ToolManager, Tool

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
    ("POST", "/approvals/{approval_id}/reconcile"): (post("/approvals/{private}/reconcile", {"reason": "核查"}), "deny"),
    ("POST", "/approvals/{approval_id}/actions/{action_id}/resolve"): (post("/approvals/{private}/actions/missing/resolve", {"version": 0, "resolution": "retry", "reason": "核对"}), "deny"),
    ("POST", "/approvals/{approval_id}/resolve"): (post("/approvals/{private}/resolve", {"note": "已人工处理"}), "deny"),
    ("GET", "/approvals/{approval_id}/audit"): (get("/approvals/{private}/audit"), "deny"),
    ("GET", "/business/employees/{employee_id}"): (get("/business/employees/1001"), "deny"),  # same id, own (empty) directory
    ("GET", "/knowledge/documents"): (get("/knowledge/documents"), "list"),
    ("DELETE", "/knowledge/documents/{document_id}"): (delete("/knowledge/documents/{doc}"), "deny"),
    ("POST", "/search"): (post("/search", query=MARK), "list"),
    ("GET", "/admin/changes"): (get("/admin/changes"), "list"),
    ("GET", "/admin/audit"): (get("/admin/audit", limit=500), "list"),
    ("GET", "/admin/sessions/revocations"): (get("/admin/sessions/revocations"), "list"),
}

# Known cross-tenant leaks: route -> (probe, kind, plan phase that closes it).
GAPS: Dict[Tuple[str, str], Tuple[Request, str, str]] = {}

# Act on the caller's own tenant only; ``test_own_scope_writes_stay_in_the_callers_tenant`` checks
# the owner's data afterwards, and the routes' own tests cover the rest.
OWN_SCOPE = {
    ("GET", "/memory/me"), ("DELETE", "/memory/me"), ("DELETE", "/memory/me/episodic/{episodic_id}"),
    ("GET", "/auth/me"), ("POST", "/auth/logout-all"), ("POST", "/admin/sessions/revoke"),
    ("POST", "/knowledge/add"), ("POST", "/knowledge/upload"), ("GET", "/knowledge/stats"),
    ("GET", "/policy"), ("POST", "/policy/revalidate"),   # caller's tenant policy; platform admins sweep all
}

# Isolation tested elsewhere with a same-subject, other-tenant caller.
COVERED = {
    ("POST", "/chat"): "test_isolation_and_history::test_private_working_and_cross_conversation_memory_are_scoped",
    ("POST", "/chat/stream"): "test_isolation_and_history::test_same_subject_other_tenant_cannot_resume_stream_or_read_trace",
    ("GET", "/chat/stream/{request_id}"): "test_isolation_and_history::test_same_subject_other_tenant_cannot_resume_stream_or_read_trace",
    ("GET", "/conversations"): "test_conversation_history::test_same_subject_in_another_tenant_sees_nothing",
    ("GET", "/conversations/{conv_id}"): "test_conversation_history::test_same_subject_in_another_tenant_sees_nothing",
    ("PATCH", "/conversations/{conv_id}"): "test_conversation_history::test_same_subject_in_another_tenant_sees_nothing",
    ("DELETE", "/conversations/{conv_id}"): "test_conversation_history::test_same_subject_in_another_tenant_sees_nothing",
    ("GET", "/trace/{request_id}"): "test_isolation_and_history::test_same_subject_other_tenant_cannot_resume_stream_or_read_trace",
    ("GET", "/trace/tool/{request_id}"): "ops._owns_trace (same check as /trace/{request_id})",
    ("GET", "/trace/tools"): "ops._owns_trace filters every row by tenant",
}

# Platform-wide by design today: no tenant data, or deployment-wide state. The note says which
# plan phase makes them per tenant.
PLATFORM = {
    ("GET", "/readyz"): "process readiness", ("GET", "/livez"): "process liveness",
    ("GET", "/health"): "", ("GET", "/auth/config"): "", ("POST", "/auth/dev-token"): "dev mode only",
    ("GET", "/metrics"): "阶段 5：内网暴露、按租户打标签",
    ("GET", "/skills"): "", ("POST", "/skills/reload"): "仅运营方租户的平台管理员（ADR 0025）",
    ("POST", "/ops/retention/run"): "同上",
}


@pytest.fixture
def world(db_url, monkeypatch):  # noqa: F811
    from it_copilot.workflow.store import WorkflowStore
    workflow = WorkflowStore(db_url)
    monkeypatch.setenv("IT_COPILOT_SEED_DEMO_DOCS", "false")
    kb = KnowledgeBase(db_url)
    tools = ToolManager("sk-local")

    async def knowledge(params, ctx=None):
        return await kb.search_handler(params, ctx)
    tools.register(Tool(name="knowledge_search", description="kb", handler=knowledge, schema=KNOWLEDGE_SEARCH_SCHEMA, cache_ttl=60))  # as in production
    sessions = SessionRevocations(workflow.engine, cache_ttl_s=0)
    for name, value in {"workflow": workflow, "worker": None,
                        "kb": kb, "tool_manager": tools, "sessions": sessions, "memory": None, "tenants": None,
                        "retention": RetentionSweeper(workflow.engine, RetentionPolicy())}.items():
        monkeypatch.setattr(state, name, value)

    private = workflow.create("ap-tenant-a", request_id="r-tenant-a", conv_id="conv-tenant-a", principal=ALICE,
                              action_requests=[access(role="reporter", reason=MARK)])
    kb.add_documents([{"title": f"{MARK} VPN SOP", "content": f"{MARK} VPN 证书排障"},
                      {"title": f"{MARK} 研发 SOP", "content": f"{MARK} 研发专用", "department": "engineering"}], tenant="default")
    sessions.revoke("default", f"dev:{MARK}", actor=ERIN.subject, reason=MARK)
    admin_audit.record(workflow.engine, ERIN, "knowledge_added", MARK, {"titles": [MARK]})
    ids = {
           "private": private["approval_id"], "private_hash": private["payload_hash"],
           "doc": next(d["document_id"] for d in kb.list_documents(tenant="default") if d["title"] == f"{MARK} VPN SOP")}
    return TestClient(api.app), ids, workflow, kb, sessions


def approval_rows(workflow):
    with workflow.engine.connect() as db:
        rows = db.execute(select(approvals.c.id, approvals.c.status, approvals.c.execution, approvals.c.version)).all()
        votes = db.execute(select(func.count()).select_from(approval_decisions)).scalar()
    return sorted(map(tuple, rows)), votes


def snapshot(workflow, kb, sessions, ids):
    """What tenant A owns, as tenant A sees it."""
    return {
        "approvals": approval_rows(workflow),
        "documents": sorted(d["document_id"] for d in kb.list_documents(tenant="default")),
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
    client, ids, workflow, kb, sessions = world
    before = snapshot(workflow, kb, sessions, ids)
    assert dict((r[0], r[1]) for r in before["approvals"][0])[ids["private"]] == "pending" and len(before["documents"]) == 2 and before["revocations"] == [f"dev:{MARK}"]
    for who in ("alice", "erin"):  # the owner's tenant sees the marked data, so the sweep would notice a leak
        # Also warms the knowledge_search result cache: an intruder's identical query must not hit it.
        assert MARK in client.post("/search", params={"query": MARK}, headers=auth(who)).text
    failures = [f for request, kind in PROBES.values() for f in probe(client, request, kind, ids)]
    assert failures == [], "\n".join(failures)
    assert snapshot(workflow, kb, sessions, ids) == before


@pytest.mark.parametrize("route", sorted(GAPS), ids=lambda r: f"{r[0]} {r[1]}")
def test_known_gap(world, route):
    request, kind, phase = GAPS[route]
    client, ids, *_ = world
    failures = probe(client, request, kind, ids)
    if failures:
        pytest.xfail(f"{phase}: " + failures[0])
    pytest.fail(f"{route} no longer leaks: move it from GAPS to PROBES")


def test_own_scope_writes_stay_in_the_callers_tenant(world):
    client, ids, workflow, kb, sessions = world
    before = snapshot(workflow, kb, sessions, ids)
    erin, alice = auth("erin", tenant="intruder"), auth("alice", tenant="intruder")
    assert client.get("/auth/me", headers=alice).json()["tenant"] == "intruder"
    assert client.post("/admin/sessions/revoke", headers=erin, json={"subject": f"dev:{MARK}", "reason": "b"}).status_code == 200
    assert client.post("/auth/logout-all", headers=alice).status_code == 200
    after = snapshot(workflow, kb, sessions, ids)
    assert after == before
    assert sorted(r["subject"] for r in sessions.list("intruder")) == sorted(["dev:alice", f"dev:{MARK}"])
    assert client.get("/auth/me", headers=auth("alice")).status_code == 200   # A's alice was not signed out
    # Knowledge flows neither way: B's upload is searchable in B only, and A's stats do not count it.
    stats = client.get("/knowledge/stats", headers=auth("alice")).json()["total_chunks"]
    added = client.post("/knowledge/add", headers=erin, json={"documents": [{"title": "B-ONLY-91C SOP", "content": "B-ONLY-91C 证书"}]})
    assert added.status_code == 200 and added.json()["total_chunks"] == 1
    assert "B-ONLY-91C" in client.post("/search", params={"query": "B-ONLY-91C"}, headers=auth("bob", tenant="intruder")).text
    for who in ("alice", "erin"):
        assert "B-ONLY-91C" not in client.post("/search", params={"query": "B-ONLY-91C"}, headers=auth(who)).text
    assert client.get("/knowledge/stats", headers=auth("alice")).json()["total_chunks"] == stats
