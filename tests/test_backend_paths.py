"""Read-path optimization must preserve evidence and independent failure handling."""

import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text

from agents.task_progress_advisor import TaskProgressAdvisor
from agents.task_runner import TaskAgentRunner
from api import state
from api.routes.chat import ChatRequest, _Stages, _run_turn
from tests.test_task_execution import fake_orchestrator
from tests.test_shared_tasks import room
from tests.test_task_product import env as task_env
from tests.test_workflow import ALICE, create
from workflow.store import WorkflowStore


env = task_env


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
        loop = asyncio.get_running_loop()

        def signal(name):
            arrived.add(name)
            if len(arrived) == 3:
                ready.set()

        async def recall(name):
            signal(name)
            await asyncio.wait_for(ready.wait(), 1)
            if name == "task" and fail_recall:
                raise RuntimeError("isolated unavailable source")
            return [name]

        def approvals(*args):
            loop.call_soon_threadsafe(signal, "approval")
            return [{"approval_id": "a", "status": "pending", "execution": "not_executed"}]

        orchestrator = fake_orchestrator(Counter())
        recognize = orchestrator.recognize_intent

        async def intent(*args, **kwargs):
            value = await recognize(*args, **kwargs)
            value.needs_knowledge = False
            return value

        orchestrator.recognize_intent = intent
        for key, value in dict(
            orchestrator=orchestrator,
            memory=None,
            kb=None,
            task_recall=SimpleNamespace(
                recall=lambda *a: recall("task"), prompt=lambda v: "TASK-CONTEXT"
            ),
            request_recall=SimpleNamespace(
                recall=lambda *a: recall("request"), prompt=lambda v: "REQUEST-CONTEXT"
            ),
            workflow=SimpleNamespace(conversation_approvals=approvals),
        ).items():
            monkeypatch.setattr(state, key, value)
        _, req, _, _ = await _run_turn(ChatRequest(message="查询进度"), ALICE, "r", "c", _Stages())
        assert arrived == {"task", "request", "approval"}
        assert ("TASK-CONTEXT" in req.context) is not fail_recall
        assert "REQUEST-CONTEXT" in req.context
        assert req.conversation_approvals[0]["status"] == "pending"

    asyncio.run(run())


def test_shared_record_includes_intent_and_advisor_usage(env):
    tasks, _, _ = env
    room(tasks)
    orchestrator = fake_orchestrator(Counter())
    recognize = orchestrator.recognize_intent

    async def intent(*args, **kwargs):
        value = await recognize(*args, **kwargs)
        value.token_usage = {"requests": 1, "input_tokens": 11, "output_tokens": 2}
        return value

    async def advice(*args):
        await asyncio.sleep(0.01)
        return None, {"requests": 1, "input_tokens": 13, "output_tokens": 4}

    orchestrator.recognize_intent = intent
    result = asyncio.run(
        TaskAgentRunner(orchestrator, lambda _: {}, advisor=SimpleNamespace(suggest=advice))(
            *tasks.context(tasks.claim())
        )
    )
    record = result["turn_record"]
    assert record["input_tokens"] == result["token_usage"]["input_tokens"] == 31
    assert record["output_tokens"] == result["token_usage"]["output_tokens"] == 9
    assert record["stage_ms"]["advice"] >= 10
    assert record["latency_ms"] >= sum(
        record["stage_ms"][k] for k in ("intent", "context", "advice")
    )


@pytest.mark.parametrize(
    "message,kind,fast",
    [
        ("收到，没有新增信息，目标和下一步均不变。", "requester", True),
        ("收到，没有新增信息，目标和下一步均不变。但现在是401。", "requester", False),
        ("收到，没有新增信息，目标和下一步均不变。", "todo_answer", False),
        ("好的，提交吧", "requester", False),
    ],
)
def test_no_information_shortcut_does_not_swallow_new_facts(message, kind, fast):
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    calls = []
    progress = {
        "goal": "排查访问",
        "steps": [{"title": "核查", "status": "working", "evidence_ids": []}],
        "blocker": "等待",
        "next_action": "等待核查",
        "brief": {"reported": []},
        "allow_reply_revision": True,
    }

    def model(messages, info):
        calls.append(1)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "draft_progress",
                    {
                        **progress,
                        "progress_changed": False,
                        "reviewed_response": "根据本轮新信息继续核查。",
                    },
                )
            ]
        )

    result, usage = asyncio.run(
        TaskProgressAdvisor(FunctionModel(model)).suggest(
            progress,
            {"tenant": "default", "title": "排查"},
            {"id": "e", "kind": kind, "text": message},
            "草稿",
            "reply",
        )
    )
    assert bool(calls) is not fast
    assert result is not None
    if fast:
        assert usage == {} and result["steps"] == progress["steps"]


def test_failed_shared_turn_counts_intent_usage_and_total_latency(env):
    from dataclasses import replace
    from workflow.task_ports import TaskTurnFailed

    tasks, _, _ = env
    room(tasks)
    orchestrator = fake_orchestrator(Counter())
    recognize, run = orchestrator.recognize_intent, orchestrator.run

    async def intent(*args, **kwargs):
        value = await recognize(*args, **kwargs)
        value.token_usage = {"requests": 1, "input_tokens": 11, "output_tokens": 2}
        return value

    async def fail(req):
        return replace(await run(req), status="failed", error_code="model_unavailable")

    orchestrator.recognize_intent, orchestrator.run = intent, fail
    with pytest.raises(TaskTurnFailed) as caught:
        asyncio.run(TaskAgentRunner(orchestrator, lambda _: {})(*tasks.context(tasks.claim())))
    record = caught.value.record
    assert record["input_tokens"] == 18 and record["output_tokens"] == 5
    assert record["latency_ms"] >= record["stage_ms"]["intent"] + record["stage_ms"]["context"]
