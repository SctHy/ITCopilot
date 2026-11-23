"""Background memory maintenance: ProfileUpdater coalescing and working-memory compression; offline."""
import asyncio
import tempfile

import fakeredis.aioredis
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from servicedesk_agent.agent.memory.conversation_memory import MemoryManager


def test_maintain_compresses_working_memory_written_by_chat_path():
    async def run():
        async def summarize(messages, info):
            assert len(messages) > 2 and messages[0].parts[0].content.startswith("VPN 还是报错")
            assert "总结" in messages[-1].parts[0].content
            return ModelResponse(parts=[TextPart("用户反复反馈 VPN-691，已建议检查账号与系统时间。")])

        with tempfile.TemporaryDirectory() as path:
            manager = MemoryManager(redis_url="redis://127.0.0.1:6399/0", database_url=f"sqlite:///{path}/memory.sqlite3", api_key="sk-local")
            manager._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
            manager.COMPRESS_AT = 15
            manager._llm = FunctionModel(summarize)
            stored = []

            async def store_episodic(*args):
                stored.append(args)

            manager._store_episodic = store_episodic
            for i in range(8):  # 与 /chat 相同的写入路径：每轮 user + assistant 两条
                await manager.commit_turn("u1", "c1", f"r{i}", f"VPN 还是报错 {i}", {"request_id": f"r{i}", "response": f"回复 {i}"},
                                          state={"knowledge_fingerprints": ["fingerprint"], "turn_count": i})
            key = manager._wm_key("u1", "c1")
            assert await manager._redis.llen(key) == 16

            await manager.maintain("u1", "c1")

            assert await manager._redis.llen(key) == 4
            assert "VPN-691" in (await manager._redis.get(manager._summary_key("u1", "c1")))
            assert await manager.get_state("u1", "c1") == {"turn_count": 7}
            assert len(stored) == 1
            await manager._redis.aclose()

    asyncio.run(run())


def test_token_watermark_uses_replayed_prompt_and_redacts_storage(tmp_path):
    async def run():
        manager = MemoryManager(database_url=f"sqlite:///{tmp_path}/memory.db")
        manager._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        for i in range(3):
            await manager.commit_turn("owner", "c", str(i), "原始问题", {"response": "答" * 800},
                                      model_prompt="背景" * 100 + " password=VerySecretCode")
        recent = await manager._get_working_memory("owner", "c")
        assert recent[0].content == "原始问题" and "VerySecretCode" not in str(recent)
        assert not manager._needs_compression(recent)
        manager.COMPRESS_MIN_TOKENS = 100
        assert manager._needs_compression(recent)
        await manager.close()
    asyncio.run(run())


def test_knowledge_fingerprints_ignore_scores_and_reinject_updated_content():
    from servicedesk_agent.agent.conversation_state import knowledge_for_turn
    hit = dict(document_id="doc", chunk=1, content="current SOP", updated_at="today")
    first, seen = knowledge_for_turn([hit], [])
    again, same = knowledge_for_turn([{**hit, "score": 0.5}], seen)
    assert same == seen and first[0]["citation"] == again[0]["citation"]
    assert hit["content"] not in again[0]["content"]
    updated, more = knowledge_for_turn([{**hit, "updated_at": "tomorrow"}], same)
    assert len(more) == 2 and updated[0]["content"] == hit["content"]


def test_history_rejects_oversize_unmatched_and_corrupt_turns_atomically():
    from pydantic_ai.messages import ModelRequest, ToolCallPart, ToolReturnPart
    from servicedesk_agent.agent.memory.context import MemoryContext, Message, MsgRole
    from servicedesk_agent.agent.memory.model_history import MAX_BYTES, decode_turn, encode_turn

    user = ModelRequest.user_text_prompt("hello", instructions="private instructions")
    call = ModelResponse(parts=[ToolCallPart("lookup", '{"otp":123456}', tool_call_id="call")])
    result = ModelRequest(parts=[ToolReturnPart("lookup", {"token": "opaque", "note": "echo 123456"}, tool_call_id="call")])
    answer = ModelResponse(parts=[TextPart("done")])
    saved = encode_turn([user, call, result, answer], "hello", None)
    assert saved and not any(raw in saved for raw in ("123456", "opaque", "private instructions"))
    assert len(decode_turn(saved)) == 4
    assert encode_turn([user, call, answer], "hello", None) is None
    assert encode_turn([user, answer, result, answer], "hello", None) is None
    assert encode_turn([user, ModelResponse(parts=[TextPart("a" * MAX_BYTES)])], "hello", None) is None
    context = MemoryContext([Message(MsgRole.USER, "hello"), Message(MsgRole.ASSISTANT, "done",
                            metadata={"model_messages": "invalid"})], [], "")
    assert [msg.parts[0].content for msg in context.model_history()] == ["hello", "done"]


def test_block_window_keeps_tool_retries_with_their_requests():
    from pydantic_ai.messages import ModelRequest, RetryPromptPart, ToolCallPart
    from servicedesk_agent.agent.memory.context import MemoryContext, Message, MsgRole
    from servicedesk_agent.agent.memory.model_history import encode_turn

    recent = []
    for i in range(8):
        prompt = f"request {i}"
        call_id = f"call-{i}"
        turn = [
            ModelRequest.user_text_prompt(prompt),
            ModelResponse(parts=[ToolCallPart("lookup", {}, tool_call_id=call_id)]),
            ModelRequest(parts=[RetryPromptPart("missing argument", tool_name="lookup", tool_call_id=call_id)]),
            ModelResponse(parts=[TextPart("done")]),
        ]
        encoded = encode_turn(turn, prompt, None)
        assert encoded
        recent.extend([Message(MsgRole.USER, prompt), Message(MsgRole.ASSISTANT, "done",
                       metadata={"model_messages": encoded})])
    history = MemoryContext(recent, [], "").model_history(limit=15)
    assert len(history) == 12
    for index in range(3):
        user, call, retry, answer = history[index * 4:index * 4 + 4]
        assert user.parts[0].content == f"request {index + 5}"
        assert call.parts[0].tool_call_id == retry.parts[0].tool_call_id == f"call-{index + 5}"
        assert answer.parts[0].content == "done"
