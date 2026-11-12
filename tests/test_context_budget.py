"""上下文预算与前缀缓存：历史按多轮消息传、助手回答截断、检索结果精简、system prompt 固定。"""
import asyncio
from datetime import datetime

from pydantic_ai.messages import ModelRequest, ModelResponse

from agents.base_agent import BaseAgent
from agents.prompting import history_messages, turn_prompt
from agents.tools import slim_hit
from agents.types import Request
from memory.conversation_memory import MemoryContext, Message, MsgRole
from tooling.executor import ToolExecutor
from tooling.tool_spec import make_tool


def _ctx(*turns):
    return MemoryContext(recent_messages=[Message(role=r, content=c, timestamp=datetime.now()) for r, c in turns],
                         relevant_history=[], user_profile={}, summary="")


def test_history_turns_truncate_assistant_deterministically_and_keep_user():
    long_answer = "结论：" + "步骤" * 500
    ctx = _ctx((MsgRole.USER, "VPN 连不上" * 100), (MsgRole.ASSISTANT, long_answer), (MsgRole.USER, "还是不行"))
    turns = ctx.history_turns(assistant_chars=50)
    assert turns[0]["content"] == "VPN 连不上" * 100
    assert turns[1]["content"] == long_answer[:50] + "…（已截断）"
    assert ctx.history_turns(assistant_chars=50) == turns
    assert "[最近对话]" not in ctx.to_prompt_text(include_recent=False) and "[最近对话]" in ctx.to_prompt_text()


def test_history_messages_alternate_and_end_with_assistant():
    messages = history_messages([
        {"role": "assistant", "content": "孤立的开场"}, {"role": "user", "content": "a"}, {"role": "user", "content": "b"},
        {"role": "assistant", "content": "c"}, {"role": "user", "content": "未回答的上一条"},
    ])
    assert [type(m) for m in messages] == [ModelRequest, ModelResponse]
    assert messages[0].parts[0].content == "a\nb" and messages[1].parts[0].content == "c"
    assert history_messages(None) == []


def test_turn_prompt_carries_dynamic_context_and_system_prompt_stays_fixed():
    agent = BaseAgent(None, "test")
    req = Request("申请 GitLab 权限", context="[会话摘要]\n之前问过 VPN", entities={"system": ["GitLab"]},
                  conversation_state={"missing_fields": ["employee_id"]})
    turn = turn_prompt(req)
    assert turn.index("[背景信息]") < turn.index("[结构化实体]") < turn.index("[待补充]") < turn.index("[用户消息]\n申请 GitLab 权限")
    assert agent._build_system_prompt(req) == agent._build_system_prompt(Request("打印机卡纸"))
    assert turn_prompt(Request("你好")) == "你好"


def test_slim_hit_drops_scores_and_long_hashes():
    hit = {"title": "VPN-809 UDP端口排障", "content": "先检查 UDP 500/4500", "score": .03, "vector_score": .7, "bm25_score": 14.3, "chunk": 0,
           "chunk_id": "9bf0225c" + "0" * 56 + ":0", "document_id": "9bf0225c" + "0" * 56, "version": "8" * 64, "source": "sop",
           "department": "engineering", "updated_at": "2026-10-28T00:00:00Z", "citation": "VPN-809 UDP端口排障 · " + "9" * 64 + ":0"}
    assert slim_hit(hit) == {"title": "VPN-809 UDP端口排障", "content": "先检查 UDP 500/4500", "updated_at": "2026-10-28",
                              "citation": "VPN-809 UDP端口排障 · 9bf0225c:0"}
    assert slim_hit("plain text") == "plain text"


def test_self_governed_tool_skips_executor_timeout_retry_and_breaker():
    calls = []

    async def flaky(req, args):
        calls.append(1)
        raise RuntimeError("inner layer already handled retries")

    spec = make_tool("search_knowledge_base", "kb", {}, flaky, self_governed=True)
    executor = ToolExecutor(read_retries=3, failure_threshold=1)

    async def run():
        for _ in range(2):
            try:
                await executor.run(spec, Request("x"), {})
            except RuntimeError:
                pass
    asyncio.run(run())
    assert len(calls) == 2 and executor.snapshot() == {}


def test_self_governed_tool_still_has_an_overall_time_limit():
    import pytest

    from tooling.executor import ToolTimeout

    async def slow(req, args):
        await asyncio.sleep(1)

    spec = make_tool("search_knowledge_base", "kb", {}, slow, self_governed=True)
    with pytest.raises(ToolTimeout):
        asyncio.run(ToolExecutor(timeout_s=.05).run(spec, Request("x"), {}))


def test_history_keeps_a_short_record_of_tools_and_approvals():
    ctx = MemoryContext(recent_messages=[
        Message(role=MsgRole.USER, content="帮我建单", timestamp=datetime.now()),
        Message(role=MsgRole.ASSISTANT, content="已提交工单审批。", timestamp=datetime.now(),
                metadata={"request_id": "r1", "tools": ["search_knowledge_base", "create_ticket"], "approval_id": "r1:approval"}),
        Message(role=MsgRole.USER, content="谢谢", timestamp=datetime.now()),
        Message(role=MsgRole.ASSISTANT, content="不客气。", timestamp=datetime.now(), metadata={"request_id": "r2", "tools": [], "approval_id": None}),
    ], relevant_history=[], user_profile={}, summary="")
    turns = ctx.history_turns()
    assert turns[1]["content"] == "已提交工单审批。\n[本轮工具：search_knowledge_base、create_ticket；已生成待审批 r1:approval]"
    assert turns[3]["content"] == "不客气。"


def test_english_message_gets_an_explicit_reply_language_hint():
    """深度评测 E16：英文提问被用中文回答；纯英文消息在本轮提示里明确要求英文回复，中英混杂不加。"""
    assert "Reply in English" in turn_prompt(Request("How do I set up MFA on my new phone? I switched to iPhone."))
    assert "Reply in English" not in turn_prompt(Request("outlook一直弹password prompt，咋整"))


def test_prefetched_knowledge_is_compact_text_with_citations():
    """预取结果在本轮消息里（不命中缓存）：紧凑文本，citation 原样保留，标题不重复。"""
    from agents.prompting import knowledge_text
    hits = [{"title": "常见错误码速查", "content": "- 401 认证失败\n- 403 权限不足", "updated_at": "2026-11-12", "citation": "常见错误码速查 · 3c963d59:0"}]
    text = knowledge_text(hits)
    assert text == "【1】citation: 常见错误码速查 · 3c963d59:0（更新 2026-11-12）\n- 401 认证失败\n- 403 权限不足"
