"""Recovery and idempotency boundaries; no external services or models."""
import asyncio

import fakeredis.aioredis
import pytest

from core.events import InMemoryEventBus, RedisEventBus, StreamConflict, request_fingerprint
from core.states import IllegalTransition
from tooling.executor import ToolExecutor
from tooling.tool_spec import make_tool
from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from workflow.store import WorkflowStore
from workflow.worker import ExecutionWorker
from tests.test_workflow import BOB, Clock, create


def test_cancelled_tool_probe_can_recover_and_late_cancel_cannot_close_new_probe():
    async def run():
        executor = ToolExecutor(failure_threshold=1, recovery_s=0, read_retries=0)
        breaker = executor.breaker("probe")
        breaker.record_failure()
        entered = asyncio.Event()

        async def hang(req, args):
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(executor.run(make_tool("probe", "", {}, hang), None, {}))
        await entered.wait()
        old_generation = breaker.generation
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert breaker.state == "open"
        assert breaker.allow() and breaker.state == "half_open"
        breaker.abandon_probe(old_generation)
        assert breaker.state == "half_open"
        breaker.record_success(breaker.generation)
        assert breaker.state == "closed"
    asyncio.run(run())


@pytest.mark.parametrize("redis", [False, True])
def test_stream_binding_and_terminal_event_are_atomic(redis):
    async def run():
        client = fakeredis.aioredis.FakeRedis(decode_responses=True) if redis else None
        bus = RedisEventBus(client) if client else InMemoryEventBus()
        try:
            fingerprint = request_fingerprint("message", "conversation")
            claims = await asyncio.gather(*(bus.claim("chat:x", "alice", fingerprint) for _ in range(5)))
            assert sum(claims) == 1
            assert await bus.owner("chat:x") == "alice"
            for content, conv in (("changed", "conversation"), ("message", "changed")):
                with pytest.raises(StreamConflict):
                    await bus.claim("chat:x", "alice", request_fingerprint(content, conv))
            # Another owner is denied by the route, without revealing a content conflict.
            assert not await bus.claim("chat:x", "bob", "different")
            await bus.publish("chat:x", "completed", {"response": "done"})
            assert await bus.status("chat:x") == "completed"
            with pytest.raises(IllegalTransition):
                await bus.publish("chat:x", "answer_delta", {"delta": "late"})
            assert len(await bus.read("chat:x", None, 0)) == 1
        finally:
            if client:
                await client.aclose()
    asyncio.run(run())


def test_worker_expires_unattended_approval_once_at_deadline(db_url):
    from datetime import datetime
    clock = Clock()
    notices = []
    store = WorkflowStore(db_url, clock=clock, notify=lambda db, notice: notices.append(notice))
    try:
        body = create(store)
        clock.t = datetime.fromisoformat(body["expires_at"]).timestamp()
        worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)))
        asyncio.run(worker.run_once())
        asyncio.run(worker.run_once())
        assert store.get("a")["status"] == "expired"
        assert store.decide("a", "approved", BOB, body["payload_hash"])["status"] == "expired"
        assert [n["event"] for n in notices] == ["expired"]
        assert store.apply_actions("a") == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_renewal_loss_cancels_work_and_parent_cancellation_is_preserved():
    from core.concurrency import LeaseLostError, run_leased
    cancelled = asyncio.Event()

    async def work():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def lost():
        return False

    with pytest.raises(LeaseLostError):
        await asyncio.wait_for(run_leased(work, lost, .01), 1)
    assert cancelled.is_set()

    async def held():
        return True

    cancelled.clear()
    task = asyncio.create_task(run_leased(work, held, .01))
    await asyncio.sleep(.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_transient_renewal_error_is_retried_but_persistent_errors_cancel_before_expiry():
    from core.concurrency import LeaseLostError, run_leased
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("database is locked")
        return True

    async def work():
        await asyncio.sleep(.1)
        return "done"

    assert await run_leased(work, flaky, .02) == "done"  # one hiccup must not cancel in-flight effects
    assert len(attempts) >= 2

    async def broken():
        raise RuntimeError("database is down")

    async def forever():
        await asyncio.Event().wait()

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(LeaseLostError):
        await asyncio.wait_for(run_leased(forever, broken, .05), 1)
    assert loop.time() - started < .15  # cancelled before the 3 * interval lease expires


def test_expired_worker_cannot_commit_or_renew_even_before_reclaim(db_url):
    from workflow.store import LeaseLost
    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    try:
        pending = create(store)
        store.decide("a", "approved", BOB, pending["payload_hash"])
        action = store.claim("worker", 1)[0]
        assert store.renew(action, 1)
        clock.t += 2
        assert not store.renew(action, 1)
        with pytest.raises(LeaseLost):
            store.succeed(action, {})
        assert store.claim("replacement", 10)[0]["attempts"] == 2
    finally:
        store.close()


@pytest.mark.asyncio
async def test_archive_outbox_survives_compression_failure_and_replays_without_new_chat(db_url):
    from memory.conversation_memory import MemoryManager
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    manager = MemoryManager(redis_url="redis://localhost:1/0", database_url=db_url, api_key="sk-local")
    manager._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def summarize(messages, info):
        return ModelResponse(parts=[TextPart("VPN 认证失败，已检查账号。")])

    manager._llm = FunctionModel(summarize)
    attempts = []

    async def unavailable(*args):
        attempts.append(args)
        return False

    manager._store_episodic = unavailable
    try:
        for i in range(8):
            await manager.commit_turn("u", "c", str(i), "VPN 问题", {"response": "答复"})
        assert await manager._compress("u", "c") == "archive_pending"
        assert await manager._redis.llen(manager._wm_key("u", "c")) == 5
        assert await manager._redis.hlen(manager._archive_key("u", "c")) == 1

        async def recovered(*args):
            attempts.append(args)
            return True

        manager._store_episodic = recovered
        await manager.flush_pending_archives()
        await manager.flush_pending_archives()
        assert await manager._redis.hlen(manager._archive_key("u", "c")) == 0
        assert len(attempts) == 2 and attempts[0] == attempts[1]
        assert not await manager._redis.smembers(manager.ARCHIVE_INDEX)  # drained archives leave the pending index
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_archives_written_before_the_pending_index_are_migrated_once(db_url):
    import json
    from memory.conversation_memory import MemoryManager
    manager = MemoryManager(redis_url="redis://localhost:1/0", database_url=db_url, api_key="sk-local")
    manager._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    stored, scans = [], []
    original_scan = manager._redis.scan

    async def scan(*args, **kwargs):
        scans.append(1)
        return await original_scan(*args, **kwargs)

    async def store(*args):
        stored.append(args)
        return True

    manager._redis.scan, manager._store_episodic = scan, store
    try:
        legacy = manager._archive_key("u", "old")  # unindexed: written by the previous release
        await manager._redis.hset(legacy, "a1", json.dumps({"text": "t", "summary": "s", "user_id": "u", "conv_id": "old"}))
        for _ in range(3):
            await manager.flush_pending_archives()
        assert len(stored) == 1 and not await manager._redis.exists(legacy)
        assert len(scans) == 1  # keyspace is scanned once for migration, never per tick afterwards
    finally:
        await manager.close()


def test_document_replacement_rolls_back_if_stale_chunk_removal_fails(db_url):
    import numpy as np
    from sqlalchemy import event
    from core.vector_store import VectorStore

    class Embedder:
        model_name = "test"

        def embed_documents(self, texts):
            return [np.array([1., 0.]) for _ in texts]

    store = VectorStore(db_url, embedder=Embedder())
    collection = store.collection("atomic")
    collection.upsert(["old"], ["original"])

    def refuse_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("DELETE"):
            raise RuntimeError("injected delete failure")

    event.listen(store.engine, "before_cursor_execute", refuse_delete)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            collection.upsert(["new"], ["replacement"], delete_ids=["old"])
        assert collection.get()["ids"] == ["old"]
    finally:
        event.remove(store.engine, "before_cursor_execute", refuse_delete)
        store.dispose()


@pytest.mark.asyncio
async def test_partial_startup_closes_acquired_resources(monkeypatch):
    from types import SimpleNamespace
    import agents.agent_orchestrator
    import api.main
    from api import state
    import memory.conversation_memory
    import monitor.telemetry
    import tooling.knowledge_base

    closed = []

    class Orchestrator:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        async def aclose(self):
            closed.append("orchestrator")

    class Memory:
        def __init__(self, **kwargs):
            pass

        async def ping(self):
            raise ConnectionError("unavailable")

        async def close(self):
            closed.append("memory")

    for key in ("lifecycle", "profile_updater", "channels", "feishu", "mcp_client", "tracing", "skills", "tool_manager", "kb", "orchestrator", "memory"):
        monkeypatch.setattr(state, key, None)
    monkeypatch.setenv("IT_COPILOT_REQUIRE_REDIS", "true")
    monkeypatch.setattr(agents.agent_orchestrator, "AgentOrchestrator", Orchestrator)
    monkeypatch.setattr(memory.conversation_memory, "MemoryManager", Memory)
    monkeypatch.setattr(tooling.knowledge_base, "KnowledgeBase", lambda url: SimpleNamespace())
    monkeypatch.setattr(monitor.telemetry, "setup_tracing", lambda: None)
    monkeypatch.setattr(monitor.telemetry, "shutdown_tracing", lambda x: closed.append("tracing"))
    with pytest.raises(RuntimeError, match="Redis"):
        async with api.main.lifespan(api.main.app):
            pytest.fail("unreachable")
    assert closed == ["memory", "orchestrator", "tracing"]
    assert state.lifecycle.state == "closed"
