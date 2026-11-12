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
    ctx = MemoryContext(recent_messages=[], relevant_history=[], user_profile={"background": "研发部", "environment": "MacBook"}, summary="")
    text = ctx.to_prompt_text()
    assert "研发" in text and "\\u" not in text


def test_profile_prompt_is_a_short_document():
    stored = MemoryManager._normalize_profile({"background": "platform 组后端工程师，新入职", "work": "看 platform 代码，用 GitLab、Jira",
                                               "environment": "公司配 MacBook Pro，手机 iPhone", "communication": "中文，回答简短"})
    text = MemoryContext(recent_messages=[], relevant_history=[], user_profile=stored, summary="").to_prompt_text()
    assert text == ("[用户画像]\n背景：platform 组后端工程师，新入职\n工作内容：看 platform 代码，用 GitLab、Jira\n"
                    "设备与环境：公司配 MacBook Pro，手机 iPhone\n沟通偏好：中文，回答简短")


def test_each_profile_field_is_capped():
    stored = MemoryManager._normalize_profile({k: "很长" * 200 for k in ("background", "work", "environment", "communication")})
    assert all(len(stored[k]) <= 80 for k in ("background", "work", "environment", "communication"))


def test_legacy_list_profile_is_upgraded():
    legacy = {"schema_version": 3, "department": "platform", "systems": ["GitLab", "Jira"], "devices": ["MacBook Pro"],
              "preferences": ["中文"], "facts": [{"key": "department", "value": "old", "status": "superseded"}], "stable_attributes": {}}
    assert MemoryManager._upgrade_profile(legacy) == {"schema_version": 4, "background": "platform", "work": "GitLab、Jira",
                                                      "environment": "MacBook Pro", "communication": "中文"}


def test_history_window_trims_in_blocks_so_prefix_stays_cacheable():
    from memory.conversation_memory import Message, MsgRole

    def first(n):
        msgs = [Message(role=MsgRole.USER if i % 2 == 0 else MsgRole.ASSISTANT, content=f"m{i}") for i in range(n)]
        turns = MemoryContext(recent_messages=msgs, relevant_history=[], user_profile={}, summary="").history_turns(limit=20)
        assert len(turns) <= 20 and turns[0]["role"] == "user"
        return turns[0]["content"]

    assert first(20) == "m0"
    # 压缩推迟时列表继续增长：21–30 条共用同一个开头，前缀缓存不被逐轮打断。
    assert {first(n) for n in range(21, 31)} == {"m10"}
    assert first(31) == "m20"


def test_profile_extraction_sees_only_what_the_employee_said():
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from evaluation.fake_model import scripted_model
    prompts = []

    async def reply(messages, info):
        prompts.append("".join(str(getattr(p, "content", "")) for m in messages for p in m.parts))
        return ModelResponse(parts=[ToolCallPart("save_profile", {"background": "platform 组后端", "communication": "中文"})])

    async def run():
        with tempfile.TemporaryDirectory() as path:
            m = manager(path)
            m._llm = scripted_model(reply)
            await m.commit_turn("u", "c", "r1", "我在 platform 组做后端", {"request_id": "r1", "response": "结论：先给结论再列步骤。ASSISTANT-STYLE"})
            assert await m.update_profile("u", "c")
            first = await m._get_profile("u")
            await m.update_profile("u", "c")
            await m._redis.aclose()
            return first

    profile = asyncio.run(run())
    assert "我在 platform 组做后端" in prompts[0] and "ASSISTANT-STYLE" not in prompts[0]
    # 第二次提炼把上一版画像作为“旧版本”交给模型修订。
    assert "背景：platform 组后端" in prompts[1]
    assert profile == {"schema_version": 4, "background": "platform 组后端", "communication": "中文"}
