"""记忆层的上下文开销：跨会话检索不重复当前会话、画像提炼节流、画像不转义中文。"""
import asyncio
import json
import tempfile
from datetime import datetime

import pytest
import fakeredis.aioredis

from it_copilot.agent.memory.conversation_memory import MemoryContext, MemoryManager


def manager(path):
    m = MemoryManager(redis_url="redis://127.0.0.1:6399/0", database_url=f"sqlite:///{path}/memory.sqlite3", api_key="sk-local")
    m._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return m


def test_trace_masking_preserves_identity_for_authorization(tmp_path):
    from dataclasses import replace
    from it_copilot.api.routes.ops import _owns_trace
    from it_copilot.infra.identity import Principal

    async def run():
        m = manager(tmp_path)
        person = Principal(subject="subject:13812345678", username="alice", department="engineering", tenant="tenant:13912345678")
        trace = {"observability": {"owner": person.subject, "tenant": person.tenant},
                 "response": "联系电话 13812345678，密码=ExampleSecret123"}
        await m.commit_turn(person.scope_key, "c", "r", "hello",
                            {"request_id": "r", "response": "done"}, trace)
        saved = await m.get_trace("r")
        assert _owns_trace(saved, person)
        assert not _owns_trace(saved, replace(person, subject="subject:13812345679"))
        assert not _owns_trace(saved, replace(person, tenant="tenant:13912345679"))
        assert "13812345678" not in saved["response"]
        assert "ExampleSecret123" not in saved["response"]
        await m._redis.aclose()
    asyncio.run(run())


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






def test_profile_in_prompt_keeps_chinese_readable():
    ctx = MemoryContext(recent_messages=[], relevant_history=[], user_profile=MemoryManager._normalize_profile({"background": "研发部", "environment": "MacBook"}), summary="")
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


@pytest.mark.parametrize("version", [3, 4, 6, None])
def test_profile_reader_rejects_unsupported_schema(version):
    with pytest.raises(ValueError, match="Profile schema must be 5"):
        MemoryManager._read_profile({"schema_version": version, "department": "platform"})


def test_profile_reader_preserves_current_field_provenance():
    profile = MemoryManager._normalize_profile({"background": "platform"}, updated_at=42, source_request_id="turn-1")
    assert MemoryManager._read_profile(profile) == profile
    assert MemoryManager._read_profile({}) == {}


def test_history_window_trims_in_blocks_so_prefix_stays_cacheable():
    from it_copilot.agent.memory.conversation_memory import Message, MsgRole

    def first(n):
        msgs = [Message(role=MsgRole.USER if i % 2 == 0 else MsgRole.ASSISTANT, content=f"m{i}") for i in range(n)]
        turns = MemoryContext(recent_messages=msgs, relevant_history=[], user_profile={}, summary="").history_turns(limit=20)
        assert len(turns) <= 20 and turns[0]["role"] == "user"
        return turns[0]["content"]

    assert first(20) == "m0"
    # 压缩推迟时列表继续增长：21–30 条共用同一个开头，前缀缓存不被逐轮打断。
    assert {first(n) for n in range(21, 31)} == {"m10"}
    assert first(31) == "m20"
