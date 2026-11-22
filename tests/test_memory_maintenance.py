"""Background memory maintenance: ProfileUpdater coalescing and working-memory compression; offline."""
import asyncio
import tempfile

import fakeredis.aioredis
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from it_copilot.agent.memory.conversation_memory import MemoryManager


def test_maintain_compresses_working_memory_written_by_chat_path():
    async def run():
        async def summarize(messages, info):
            return ModelResponse(parts=[TextPart("用户反复反馈 VPN-691，已建议检查账号与系统时间。")])

        with tempfile.TemporaryDirectory() as path:
            manager = MemoryManager(redis_url="redis://127.0.0.1:6399/0", database_url=f"sqlite:///{path}/memory.sqlite3", api_key="sk-local")
            manager._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
            manager._llm = FunctionModel(summarize)
            stored = []

            async def store_episodic(*args):
                stored.append(args)

            manager._store_episodic = store_episodic
            for i in range(8):  # 与 /chat 相同的写入路径：每轮 user + assistant 两条
                await manager.commit_turn("u1", "c1", f"r{i}", f"VPN 还是报错 {i}", {"request_id": f"r{i}", "response": f"回复 {i}"})
            key = manager._wm_key("u1", "c1")
            assert await manager._redis.llen(key) == 16

            await manager.maintain("u1", "c1")

            assert await manager._redis.llen(key) == 5
            assert "VPN-691" in (await manager._redis.get(manager._summary_key("u1", "c1")))
            assert len(stored) == 1
            await manager._redis.aclose()

    asyncio.run(run())
