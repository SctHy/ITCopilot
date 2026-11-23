"""Read-path optimization must preserve evidence and independent failure handling."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, text

from tests.runtime import state
from servicedesk_agent.api.routes.chat import ChatRequest, _Stages, _run_turn
from tests.test_workflow import ALICE, create
from servicedesk_agent.workflow.store import WorkflowStore





def test_batch_matches_individual_views_with_constant_queries(db_url):
    store = WorkflowStore(db_url)
    for i in range(8):
        create(store, str(i))
    ids = [str(i) for i in range(8)]
    expected = [store.get(i) for i in ids]
    queries = []

    def capture(conn, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append(statement)

    event.listen(store.engine, "before_cursor_execute", capture)
    try:
        assert store.get_many(ids) == expected
        assert len(queries) == 4
        assert store.get_many([]) == []
    finally:
        event.remove(store.engine, "before_cursor_execute", capture)
    if store.engine.dialect.name == "sqlite":
        with store.engine.connect() as db:
            plan = str(
                db.execute(
                    text(
                        "EXPLAIN QUERY PLAN SELECT * FROM approvals WHERE tenant='default' "
                        "AND requester='dev:alice' AND conv_id='c' ORDER BY created_at DESC,id LIMIT 20"
                    )
                ).all()
            )
        assert "ix_approvals_conversation" in plan
        assert "TEMP B-TREE" not in plan


@pytest.mark.parametrize("fail_recall", [False, True])
def test_private_context_reads_overlap_and_keep_other_results(monkeypatch, fail_recall):
    async def run():
        arrived = set()
        ready = asyncio.Event()

        def signal(name):
            arrived.add(name)
            if len(arrived) == 2:
                ready.set()

        async def recall(name):
            signal(name)
            await asyncio.wait_for(ready.wait(), 1)
            if name == "request" and fail_recall:
                raise RuntimeError("isolated unavailable source")
            return [name]

        async def run_repo(operation, *args):
            await recall("approval")
            return operation(*args)

        def approvals(*args):
            return [{"approval_id": "a", "status": "pending", "execution": "not_executed"}]

        from servicedesk_agent.agent.agent_orchestrator import AgentOrchestrator
        orchestrator = AgentOrchestrator("sk-local")
        from servicedesk_agent.agent.intent.taxonomy import IntentCategory, UrgencyLevel

        async def intent(*args, **kwargs):
            value = SimpleNamespace(intent=IntentCategory.PERMISSION_REQUEST, intent_group="access", confidence=1, urgency=UrgencyLevel.LOW, entities={}, missing_slots=[], recognition_source="test", requested_actions=frozenset())
            value.needs_knowledge = False
            return value

        orchestrator.recognize_intent = intent
        orchestrator.run = AsyncMock(return_value=None)
        for key, value in dict(
            orchestrator=orchestrator,
            memory=None,
            kb=None,
            request_recall=SimpleNamespace(
                recall=lambda *a: recall("request"), prompt=lambda v: "REQUEST-CONTEXT"
            ),
            workflow=SimpleNamespace(conversation_approvals=approvals, run=run_repo),
        ).items():
            monkeypatch.setattr(state, key, value)
        _, req, _, _ = await _run_turn(state, ChatRequest(message="查询进度"), ALICE, "r", "c", _Stages())
        assert arrived == {"request", "approval"}
        assert ("REQUEST-CONTEXT" in req.context) is not fail_recall
        assert req.conversation_approvals[0]["status"] == "pending"

    asyncio.run(run())
