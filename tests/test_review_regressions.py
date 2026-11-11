"""Regressions for data integrity and transport boundaries found in the project audit."""
import json

import fakeredis.aioredis
import pytest

from core.env import env_float
from core.events import InMemoryEventBus, RedisEventBus, MAX_PAYLOAD_CHARS, STREAM_TTL_S
from core.vector_store import VectorStore
from tests.test_workflow import ALICE, BOB, access
from workflow.adapters import AdapterRegistry
from workflow.adapters.gitea import GiteaAdapter
from workflow.adapters.local import LocalDirectoryAdapter
from workflow.store import WorkflowStore
from workflow.worker import ExecutionWorker
import httpx2


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-Infinity", "", "oops"])
def test_invalid_timeouts_fall_back(monkeypatch, value):
    monkeypatch.setenv("IT_TEST_TIMEOUT", value)
    assert env_float("IT_TEST_TIMEOUT", 3.5) == 3.5


@pytest.mark.parametrize("ids,docs,metadata", [
    (["a", "b"], ["one"], None),
    (["a"], ["one", "two"], None),
    (["a"], ["one"], []),
    ([], ["one"], None),
    (["a", "b"], ["one", "two"], [{}]),
])
def test_vector_input_length_mismatch_never_partially_writes(db_url, ids, docs, metadata):
    collection = VectorStore(db_url).collection("length-check")
    with pytest.raises(ValueError, match="等长"):
        collection.upsert(ids, docs, metadata)
    assert collection.count() == 0


def test_incomplete_embedding_batch_never_partially_writes(db_url):
    class Incomplete:
        model_name = "incomplete"

        def embed_documents(self, texts):
            return []

    collection = VectorStore(db_url, embedder=Incomplete()).collection("embedding-check")
    with pytest.raises(ValueError, match="embedding"):
        collection.upsert(["a"], ["one"])
    assert collection.count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("redis", [False, True])
async def test_long_completed_response_is_replayable_and_redacted(redis):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True) if redis else None
    bus = RedisEventBus(client) if redis else InMemoryEventBus()
    try:
        await bus.claim("chat:long", "alice")
        if client:
            await client.expire("events:owner:chat:long", 1)
        payload = {"request_id": "long", "response": "长回答" * MAX_PAYLOAD_CHARS,
                   "approval_id": "a", "secret": "must-not-leak"}
        await bus.publish("chat:long", "completed", payload)
        events = await bus.read("chat:long", None, 0)
        assert events[0][2]["response"] == payload["response"]
        assert events[0][2]["approval_id"] == "a"
        assert events[0][2]["secret"] == "[REDACTED]"
        assert await bus.read("chat:long", events[0][0], 0) == []
        await bus.claim("chat:tools", "alice")
        await bus.publish("chat:tools", "tool_finished", payload)
        assert (await bus.read("chat:tools", None, 0))[0][2]["truncated"]
        if client:
            assert await client.ttl("events:owner:chat:long") > STREAM_TTL_S - 5
    finally:
        if client:
            await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["admin", "owner", "custom-role"])
async def test_existing_gitea_permission_is_never_downgraded(db_url, level):
    writes = []

    def handler(request):
        if request.method != "GET":
            writes.append(json.loads(request.content) if request.content else request.method)
            return httpx2.Response(204)
        if request.url.path.endswith("/permission"):
            return httpx2.Response(200, json={"permission": level})
        if request.url.path.endswith("/collaborators/alice"):
            return httpx2.Response(204)
        return httpx2.Response(200, json={})

    store = WorkflowStore(db_url)
    adapter = GiteaAdapter("http://gitea", "t", {"gitlab": "it/platform"}, store.username_of,
                           transport=httpx2.MockTransport(handler))
    registry = AdapterRegistry(LocalDirectoryAdapter(store.engine), {"gitlab": adapter})
    store.route = lambda action: registry.for_action(action).name
    pending = store.create("a", request_id="r", conv_id="c", principal=ALICE, action_requests=[access()])
    store.decide("a", "approved", BOB, pending["payload_hash"])
    try:
        worker = ExecutionWorker(store, registry, max_attempts=1)
        await worker.run_until_idle()
        assert writes == []
        body = store.get("a")
        assert body["revocations"] == []
        if level == "custom-role":
            assert body["execution"] == "manual_review"
        else:
            assert store.employee("1001")["access_ledger"][0]["status"] == "preexisting"
    finally:
        await adapter.close()
        store.close()
