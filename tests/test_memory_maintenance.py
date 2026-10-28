"""Background memory maintenance: ProfileUpdater coalescing and working-memory compression; offline."""
import asyncio
import tempfile

import fakeredis.aioredis
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from core.concurrency import ProfileUpdater
from memory.conversation_memory import MemoryManager


def test_profile_updater_coalesces_backlog_per_user():
    async def run():
        release = asyncio.Event()
        calls = []

        class Memory:
            async def maintain(self, user_id, conv_id):
                calls.append((user_id, conv_id))
                await release.wait()

        updater = ProfileUpdater(Memory(), workers=1)
        updater.start()
        updater.submit("u1", "c1")
        await asyncio.sleep(0)  # worker 取走 c1 并阻塞
        for conv in ("c2", "c3"):
            updater.submit("u1", conv)
        updater.submit("u2", "c9")
        release.set()
        for _ in range(50):
            if len(calls) >= 3:
                break
            await asyncio.sleep(0.01)
        await updater.close()
        assert calls == [("u1", "c1"), ("u1", "c3"), ("u2", "c9")]

    asyncio.run(run())


def test_maintain_compresses_working_memory_written_by_chat_path():
    async def run():
        async def summarize(messages, info):
            return ModelResponse(parts=[TextPart("用户反复反馈 VPN-691，已建议检查账号与系统时间。")])

        with tempfile.TemporaryDirectory() as path:
            manager = MemoryManager(redis_url="redis://127.0.0.1:6399/0", chroma_host="127.0.0.1", chroma_port=8999, chroma_path=path, api_key="sk-local")
            manager._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
            manager._llm = FunctionModel(summarize)
            stored, profiles = [], []

            async def store_episodic(*args):
                stored.append(args)

            async def update_profile(user_id, conv_id):
                profiles.append((user_id, conv_id))

            manager._store_episodic = store_episodic
            manager.update_profile = update_profile
            for i in range(8):  # 与 /chat 相同的写入路径：每轮 user + assistant 两条
                await manager.commit_turn("u1", "c1", f"r{i}", f"VPN 还是报错 {i}", {"request_id": f"r{i}", "response": f"回复 {i}"})
            key = manager._wm_key("u1", "c1")
            assert await manager._redis.llen(key) == 16

            await manager.maintain("u1", "c1")

            assert await manager._redis.llen(key) == 5
            assert "VPN-691" in (await manager._redis.get(manager._summary_key("u1", "c1")))
            assert len(stored) == 1 and profiles == [("u1", "c1")]
            await manager._redis.aclose()

    asyncio.run(run())
