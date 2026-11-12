"""记忆层的上下文开销：跨会话检索不重复当前会话、画像提炼节流、画像不转义中文。"""
import asyncio
import json
import tempfile
from datetime import datetime

import fakeredis.aioredis

from memory.conversation_memory import MemoryContext, MemoryManager


def manager(path):
    m = MemoryManager(redis_url="redis://127.0.0.1:6399/0", database_url=f"sqlite:///{path}/memory.sqlite3", api_key="sk-local")
    m._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return m


def test_episodic_recall_skips_the_current_conversation():
    async def run():
        with tempfile.TemporaryDirectory() as path:
            m = manager(path)
            meta = {"intent": "vpn_issue", "intent_group": "network", "entities": {}}
            await m.store_episodic_turn("u", "now", "r1", "VPN 报错 691", "检查账号锁定", meta)
            await m.store_episodic_turn("u", "last-week", "r2", "VPN 报错 691 怎么办", "上周是证书过期", meta)
            await m.store_episodic_turn("someone-else", "x", "r3", "VPN 报错 691", "别人的记录", meta)
            hits = await m._search_episodic("u", "now", "VPN 691", intent_group="network")
            await m._redis.aclose()
            return hits

    hits = asyncio.run(run())
    assert len(hits) == 1 and "证书过期" in hits[0]


def _push_turn(m, key, n):
    return m._redis.lpush(key, json.dumps({"role": "user", "content": f"q{n}", "ts": datetime.now().isoformat()}),
                          json.dumps({"role": "assistant", "content": f"a{n}", "ts": datetime.now().isoformat()}))


def test_profile_extraction_runs_once_per_three_turns_and_only_successes_advance():
    async def run():
        with tempfile.TemporaryDirectory() as path:
            m = manager(path)
            key = m._wm_key("u", "c")
            outcomes, calls = iter([False, True]), []

            async def update_profile(user_id, conv_id):
                calls.append(await m._redis.llen(key))
                return next(outcomes)

            async def no_compress(user_id, conv_id):
                return None

            m.update_profile, m._compress = update_profile, no_compress
            for turn in range(1, 6):
                await _push_turn(m, key, turn)
                await m.maintain("u", "c")
            await m._redis.aclose()
            return calls

    # 第 3 轮到期但提炼失败，计数不推进，第 4 轮重试并成功；第 5 轮只新增 2 条，不再提炼。
    assert asyncio.run(run()) == [6, 8]


def test_profile_counter_restarts_after_compression_and_keys_do_not_collide():
    async def run():
        with tempfile.TemporaryDirectory() as path:
            m = manager(path)
            key = m._wm_key("u", "c")
            for turn in range(8):
                await _push_turn(m, key, turn)
            await m._redis.set(m._profile_mark_key("u", "c"), 16)
            await m._redis.ltrim(key, 0, 4)                 # 压缩后只剩 5 条
            before = await m._profile_due("u", "c")
            await m._redis.lpush(key, "{}")
            after = await m._profile_due("u", "c")
            await m._redis.aclose()
            return before, after

    assert asyncio.run(run()) == (None, 6)
    assert MemoryManager._profile_mark_key("a:b", "c") != MemoryManager._profile_mark_key("a", "b:c")


def test_profile_in_prompt_keeps_chinese_readable():
    ctx = MemoryContext(recent_messages=[], relevant_history=[], user_profile={"department": "研发", "devices": ["MacBook"]}, summary="")
    text = ctx.to_prompt_text()
    assert "研发" in text and "\\u" not in text
