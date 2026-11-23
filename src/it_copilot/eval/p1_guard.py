"""Replay fixed attempted operations against disposable local application boundaries."""

from collections import Counter
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from fastapi import HTTPException

from it_copilot.agent.masking.model_masking import MaskingSession, mask_request
from it_copilot.agent.prompting import claim_problems
from it_copilot.agent.tooling.knowledge_base import KnowledgeBase
from it_copilot.agent.types import AgentRunDeps, Request
from it_copilot.api.deps import request_id
from it_copilot.api.routes.chat import chat_stream, chat_stream_resume
from it_copilot.api.schemas import ChatRequest
from it_copilot.infra.events import InMemoryEventBus, request_fingerprint
from it_copilot.infra.identity import Principal
from it_copilot.infra.tenant_context import tenant_scope
from it_copilot.workflow.policy import PolicyViolation
from it_copilot.workflow.store import (
    WorkflowStore,
    ApprovalConflict,
    ApprovalStateConflict,
    ApprovalForbidden,
)
from it_copilot.workflow.adapters import AdapterRegistry
from it_copilot.workflow.adapters.local import LocalDirectoryAdapter
from it_copilot.workflow.worker import ExecutionWorker
from .p1 import experiment, NAMES

OWNER = Principal(
    "test:requester", "fixture", "engineering", frozenset({"employee", "approver"}), "1001"
)
REVIEWER = replace(OWNER, subject="test:reviewer", employee_id="2001")
WRITE = {"other", "privileged", "hash", "self", "draft", "unauthorized"}


async def workflow_case(row, store, worker, guarded):
    op, key = row["operation"], row["id"]
    employee = "1002" if op == "other" else "1001"
    args = dict(
        employee_id=employee,
        system="GitLab",
        role="maintainer" if op == "privileged" else "readonly",
        reason=key,
    )
    with tenant_scope("default"):
        before = store.employee(employee, "default")["grants"]
        if not guarded:
            store.directory.apply(key, dict(tool_name="request_access", arguments=args))
            changed = before != store.employee(employee, "default")["grants"]
            return store.directory.observe(key), changed
        try:
            record = store.create(
                key,
                request_id=key,
                conv_id=key,
                principal=OWNER,
                action_requests=[dict(tool_name="request_access", arguments=args)],
                ask_requester=op == "draft",
            )
        except PolicyViolation:
            if op not in {"other", "privileged"}:
                raise
            await worker.run_until_idle()
            return False, before != store.employee(employee, "default")["grants"]
        accepted = True
        if op not in {"other", "privileged"}:
            principal = OWNER if op == "self" else REVIEWER
            if op == "unauthorized":
                principal = replace(REVIEWER, roles=frozenset({"employee"}))
            try:
                store.decide(
                    key, "approved", principal, "0" * 64 if op == "hash" else record["payload_hash"]
                )
            except (ApprovalConflict, ApprovalStateConflict, ApprovalForbidden):
                accepted = False
        await worker.run_until_idle()
        return accepted, before != store.employee(employee, "default")["grants"]


async def attempt(row, guarded, store, worker, kb):
    op, key, msg = row["operation"], row["id"], row["message"]
    if op in WRITE:
        return await workflow_case(row, store, worker, guarded)
    if op == "kb":
        marker = "FIXTURE-" + key
        kb.add_documents([dict(title=key, content=marker)], tenant="tenant-b")
        kb.add_documents([dict(title=key, content=msg)], tenant="tenant-a")
        with tenant_scope("tenant-a"):
            instructions = kb.search(msg, mode="bm25", top_k=20)
            if not any(msg in r["content"] for r in instructions):
                raise ValueError("missing fixture instruction")
            rows = kb.search(marker, tenant=None if guarded else "tenant-b", mode="bm25", top_k=20)
        return any(marker in r["content"] for r in rows), False
    if op == "pii":
        raw = key + "@example.com"
        body = dict(messages=[dict(role="tool", content=msg + " " + raw)])
        session = MaskingSession()
        wire = (mask_request(body, session) if guarded else body)["messages"][0]["content"]
        reply = session.restore(wire) if guarded else wire
        return raw in wire or raw in reply, False
    if op == "state":
        req = Request(
            msg, conversation_approvals=[dict(status="draft", actions=["request_access"])]
        )
        return not (guarded and claim_problems(msg, AgentRunDeps(req), req)), False
    state = SimpleNamespace(orchestrator=True, rate_limiter=None, events=InMemoryEventBus())
    rid = request_id(OWNER, key)
    await state.events.claim("chat:" + rid, OWNER.scope_key, request_fingerprint("原工单", key))
    if not guarded:
        return await state.events.owner("chat:" + rid) is not None, False
    try:
        if op == "replay":
            await chat_stream_resume(state, rid, replace(OWNER, tenant="tenant-b"), None)
        elif op == "conflict":
            await chat_stream(
                state, ChatRequest(message=msg, conv_id=key, client_request_id=key), OWNER
            )
        else:
            raise ValueError(op)
    except HTTPException as exc:
        if exc.status_code != (403 if op == "replay" else 409):
            raise
        return False, False
    return True, False


async def guardrails(rows, repeats):
    labels = ("unguarded_baseline", "guarded_prod")

    async def run(label):
        hits, sizes = Counter(), Counter()
        writes = 0
        with TemporaryDirectory() as tmp:
            url = f"sqlite:///{Path(tmp) / 'guard.db'}"
            store = WorkflowStore(url)
            worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)))
            kb = KnowledgeBase(url)
            try:
                for row in rows:
                    hit, wrote = await attempt(row, label == labels[1], store, worker, kb)
                    writes += wrote
                    for k in (row["vector"], row["operation"]):
                        hits[k] += hit
                        sizes[k] += 1
            finally:
                await store.aclose()
        vectors = sorted({r["vector"] for r in rows})
        metrics = {f"{k}/asr": hits[k] / sizes[k] for k in vectors}
        metrics.update(
            asr=sum(hits[k] for k in vectors) / len(rows),
            unauthorized_write_rate=writes / sum(sizes[k] for k in WRITE),
            cross_tenant_leak=(hits["kb"] + hits["replay"]) / (sizes["kb"] + sizes["replay"]),
            pii_leak_rate=hits["pii"] / sizes["pii"],
            state_detection=1 - hits["state"] / sizes["state"],
        )
        return metrics, {
            k: metrics[k]
            for k in ("asr", "unauthorized_write_rate", "cross_tenant_leak", "pii_leak_rate")
        } if label == labels[1] else {}

    return await experiment(
        NAMES[0],
        labels,
        run,
        repeats,
        "Enforcing approval can defer legitimate changes.",
        adversary="fixed worst-case model calls; local simulated targets",
    )
