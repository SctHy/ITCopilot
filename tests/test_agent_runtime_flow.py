"""BaseAgent 运行链路：实时正文流、注入警示包裹、缓存 token 计量、消息结构。模型用脚本化的 FunctionModel。"""
import asyncio

from prometheus_client import REGISTRY
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from agents.agent_orchestrator import AgentOrchestrator
from agents.types import Request
from evaluation.fake_model import scripted_model
from tests.conftest import with_read_tool

FINAL = "VPN-691 是身份认证失败：先确认账号未锁定，再检查系统时间与客户端版本；仍失败请回复我建工单。"


def make_agent(reply, *, streaming=True):
    agent = with_read_tool(AgentOrchestrator("sk-local")).agent
    agent._llm = scripted_model(reply) if streaming else FunctionModel(reply)
    return agent


def returns(messages):
    return [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]


async def diagnose_then_answer(messages, info):
    if not returns(messages):
        return ModelResponse(parts=[TextPart("我先查一下这个错误码。"), ToolCallPart("diagnose", {"code": "VPN-691"})])
    return ModelResponse(parts=[TextPart(FINAL)])


class Events:
    def __init__(self):
        self.items = []

    async def __call__(self, event, data):
        self.items.append((event, data))

    def names(self):
        return [e for e, _ in self.items]


def test_streaming_pushes_preamble_reset_then_final_answer_in_order():
    events = Events()
    agent = make_agent(diagnose_then_answer)
    response = asyncio.run(agent.handle(Request("VPN 报错 VPN-691", emit=events, stream_answer=True)))
    names = events.names()
    assert response.success and response.content == FINAL
    reset = names.index("answer_reset")
    assert names.index("answer_delta") < reset < names.index("tool_started") < names.index("tool_finished")
    before = "".join(d["delta"] for e, d in events.items[:reset] if e == "answer_delta")
    after = "".join(d["delta"] for e, d in events.items[reset:] if e == "answer_delta")
    assert before == "我先查一下这个错误码。" and after == FINAL
    # 攒批推送：除每轮最后一段外，每条至少 24 个字符。
    final_deltas = [d["delta"] for e, d in events.items[reset:] if e == "answer_delta"]
    assert len(final_deltas) < len(FINAL) / 8 and all(len(d) >= 24 for d in final_deltas[:-1])


def test_without_stream_flag_no_answer_events_and_non_streaming_model_is_enough():
    events = Events()
    agent = make_agent(diagnose_then_answer, streaming=False)
    response = asyncio.run(agent.handle(Request("VPN 报错 VPN-691", emit=events)))
    assert response.success and response.content == FINAL
    assert "answer_delta" not in events.names() and "answer_reset" not in events.names()
    assert events.names().count("tool_started") == 1


def test_messages_have_static_system_history_turns_then_dynamic_user_turn():
    seen = []

    async def reply(messages, info):
        seen.append(messages)
        return ModelResponse(parts=[TextPart("好的")])

    agent = make_agent(reply, streaming=False)
    history = [{"role": "user", "content": "VPN 连不上"}, {"role": "assistant", "content": "请提供错误码。"}]
    asyncio.run(agent.handle(Request("报错 VPN-691", context="[会话摘要]\n用户在家办公", history=history, entities={"error_code": ["VPN-691"]})))
    first, second, third = seen[0]
    assert isinstance(first, ModelRequest) and first.parts[0].content == "VPN 连不上"
    assert isinstance(second, ModelResponse) and second.parts[0].content == "请提供错误码。"
    current = [p for p in third.parts if isinstance(p, UserPromptPart)][0].content
    assert "规则：" in third.instructions and "VPN-691" not in third.instructions  # 动态内容不进 system
    assert current.startswith("[背景信息]") and current.endswith("[用户消息]\n报错 VPN-691") and "[最近对话]" not in current


def test_cache_read_tokens_are_reported_and_counted():
    async def reply(messages, info):
        return ModelResponse(parts=[TextPart("好的")], usage=RequestUsage(input_tokens=1000, output_tokens=20, cache_read_tokens=800))

    agent = make_agent(reply, streaming=False)
    labels = {"model": agent._model, "direction": "cache_read"}
    before = REGISTRY.get_sample_value("it_copilot_llm_tokens_total", labels) or 0
    response = asyncio.run(agent.handle(Request("你好")))
    assert response.token_usage == {"requests": 1, "input_tokens": 1000, "output_tokens": 20, "cache_read_tokens": 800}
    after = REGISTRY.get_sample_value("it_copilot_llm_tokens_total", labels)
    assert after == before + 800


def _claiming_model(calls, *, correct_with_tool):
    async def reply(messages, info):
        calls.append(messages)
        last = [p for p in messages[-1].parts if isinstance(p, UserPromptPart)]
        if last and "[系统核对]" in last[0].content and correct_with_tool:
            return ModelResponse(parts=[ToolCallPart("create_ticket", {"title": "VPN-691", "description": "已诊断"})])
        if returns(messages) and any(r.tool_name == "create_ticket" for r in returns(messages)):
            return ModelResponse(parts=[TextPart("工单已提交待审批。")])
        if last and "[系统核对]" in last[0].content:
            return ModelResponse(parts=[TextPart("更正：本轮还没有提交工单，需要的话请告诉我。")])
        return ModelResponse(parts=[TextPart("按你的要求，我已创建工单待审批。")])
    return reply


def _alice():
    from core.identity import Principal
    return Principal("dev:alice", "alice", "engineering", frozenset({"employee"}), "1001")


def test_unbacked_write_claim_gets_one_correction_that_can_call_the_tool():
    calls = []
    agent = make_agent(_claiming_model(calls, correct_with_tool=True), streaming=False)
    response = asyncio.run(agent.handle(Request("VPN-691，帮我建单", conversation_state={}, principal=_alice())))
    assert [t["tool_name"] for t in response.tool_traces] == ["create_ticket"] and response.tool_traces[0]["success"]
    assert response.content == "工单已提交待审批。" and len(calls) == 3


def test_unbacked_write_claim_is_corrected_in_words_when_no_tool_is_called():
    calls = []
    agent = make_agent(_claiming_model(calls, correct_with_tool=False), streaming=False)
    response = asyncio.run(agent.handle(Request("VPN-691 怎么办")))
    assert response.content.startswith("更正") and response.tool_traces == [] and len(calls) == 2


def test_claims_backed_by_this_or_an_earlier_turn_are_not_second_guessed():
    calls = []

    async def backed(messages, info):
        calls.append(messages)
        if not returns(messages):
            return ModelResponse(parts=[ToolCallPart("create_ticket", {"title": "VPN-691", "description": "已诊断"})])
        return ModelResponse(parts=[TextPart("已为你创建工单待审批。")])

    asyncio.run(make_agent(backed, streaming=False).handle(Request("建单", conversation_state={}, principal=_alice())))
    assert len(calls) == 2

    calls.clear()
    history = [{"role": "user", "content": "建单"}, {"role": "assistant", "content": "好的。\n[本轮工具：create_ticket；已生成待审批 r1:approval]"}]
    asyncio.run(make_agent(_claiming_model(calls, correct_with_tool=False), streaming=False).handle(Request("进度呢", history=history)))
    assert len(calls) == 1


def test_new_write_request_needs_this_turns_evidence_even_after_an_earlier_submission():
    calls = []
    agent = make_agent(_claiming_model(calls, correct_with_tool=True), streaming=False)
    history = [{"role": "user", "content": "申请 GitLab developer"}, {"role": "assistant", "content": "已提交。\n[本轮工具：request_access；已生成待审批 r1:approval]"}]
    response = asyncio.run(agent.handle(Request("改成只读，重新提交", history=history, conversation_state={}, principal=_alice(),
                                                allowed_writes=frozenset({"create_ticket"}))))
    assert len(calls) == 3 and [t["tool_name"] for t in response.tool_traces] == ["create_ticket"]


# ── 回答与本轮写操作结果的一致性（来自 2026-11-12 深度评测的失败形态）────────────────

def test_failed_write_reported_as_success_is_corrected_with_the_failure_reason():
    """t2-08：reset_password 返回失败，回答却说“已生成”。纠正提示要带上失败原因。"""
    calls, prompts = [], []

    async def reply(messages, info):
        calls.append(messages)
        last = [p for p in messages[-1].parts if isinstance(p, UserPromptPart)]
        if last and "[系统核对]" in last[0].content:
            prompts.append(last[0].content)
            return ModelResponse(parts=[TextPart("密码重置没有提交成功：这一轮没有确认要办理。需要的话请回复确认。")])
        if not returns(messages):
            return ModelResponse(parts=[ToolCallPart("reset_password", {"employee_id": "1001"})])
        return ModelResponse(parts=[TextPart("**结论：** 密码重置待审批请求已生成，审批通过后由身份平台核验。")])

    response = asyncio.run(make_agent(reply, streaming=False).handle(
        Request("确认重置密码", conversation_state={}, principal=_alice(), allowed_writes=frozenset({"create_ticket"}))))
    assert [t["success"] for t in response.tool_traces] == [False]
    assert len(calls) == 3 and "调用失败" in prompts[0] and "不要提到" in prompts[0]
    assert response.content.startswith("密码重置没有提交成功")


def test_reply_still_claiming_after_correction_gets_a_deterministic_note():
    """c2-grant-status：没调写工具却说“已按你的要求提交”；模型改不过来时追加以审批记录为准的说明。"""
    calls = []

    async def stubborn(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[TextPart("已按你的要求提交待审批请求：工号 1002，系统 VPN，角色 readonly。")])

    response = asyncio.run(make_agent(stubborn, streaming=False).handle(
        Request("申请 VPN 只读权限，顺便查下 corporate-vpn 状态", conversation_state={}, principal=_alice(),
                allowed_writes=frozenset({"request_access"}))))
    assert len(calls) == 2 and response.tool_traces == []
    assert response.content.endswith("【以审批记录为准】本轮申请实际没有提交成功。")


def test_withdraw_claim_without_a_withdraw_tool_is_corrected():
    """m-cancel-then-ask：用户说“算了先不申请”，回答“已不再推进”，但待审批单仍有效。"""
    prompts = []

    async def reply(messages, info):
        last = [p for p in messages[-1].parts if isinstance(p, UserPromptPart)]
        if last and "[系统核对]" in last[0].content:
            prompts.append(last[0].content)
            return ModelResponse(parts=[TextPart("刚才的 GitLab 申请仍在待审批，如需撤回请在审批卡片上点“撤回”。")])
        return ModelResponse(parts=[TextPart("403 表示权限不足。你刚才的 GitLab 只读申请已按你的意思不再推进，需要时再说。")])

    history = [{"role": "user", "content": "工号1001，申请 GitLab 只读权限，理由代码审阅。"},
               {"role": "assistant", "content": "已提交。\n[本轮工具：request_access；已生成待审批 r1:approval]"}]
    response = asyncio.run(make_agent(reply, streaming=False).handle(Request("算了先不申请，帮我解释下 403 错误。", history=history)))
    assert prompts and "撤回" in prompts[0] and response.content.startswith("刚才的 GitLab 申请仍在待审批")

    # l-two-requests-one-cancelled：之前从没提交过，“不再推进”是实话，不能纠正成“仍在待审批”。
    prompts.clear()
    never = [{"role": "user", "content": "GitLab 只读也想要，先别提交。"}, {"role": "assistant", "content": "好的，先不提交。\n[本轮工具：无]"}]
    response = asyncio.run(make_agent(reply, streaming=False).handle(Request("GitLab 那个不要了。", history=never)))
    assert not prompts and "不再推进" in response.content


def test_restating_a_past_submission_is_left_alone_when_no_write_was_asked_this_turn():
    """t-past-ticket：用户说“昨天让你们建过单”，意图识别判定本轮没要求写操作；复述不应被“纠正”成“当时没提交成功”。"""
    calls = []

    async def reply(messages, info):
        calls.append(messages)
        return ModelResponse(parts=[TextPart("昨天的建单已提交，审批通过后会有工单号；显示器黑屏一般按 P3 处理。")])

    asyncio.run(make_agent(reply, streaming=False).handle(Request("我昨天已经让你们建过显示器黑屏的单了，一般多久处理？",
                                                                  allowed_writes=frozenset())))
    assert len(calls) == 1


def test_password_pasted_by_the_user_is_not_echoed_back():
    """s-password-pasted：回答里原样复述了用户贴出的密码。"""
    async def echo(messages, info):
        return ModelResponse(parts=[TextPart("我不能核验密码明文，请立即在自助门户把 `Abc!2026x` 改成新密码。")])

    response = asyncio.run(make_agent(echo, streaming=False).handle(Request("我新密码是 Abc!2026x，帮我确认下它符合复杂度要求吗？")))
    assert "Abc!2026x" not in response.content and "`******`" in response.content


def test_forged_execution_note_is_stripped_and_honest_failure_gets_no_extra_note():
    """t2-13：模型照抄历史里的“[本轮工具：…已生成待审批 …]”格式伪造执行记录；已如实说明失败时不再追加说明。"""
    async def reply(messages, info):
        if not returns(messages):
            return ModelResponse(parts=[ToolCallPart("reset_password", {"employee_id": "1001"})])
        return ModelResponse(parts=[TextPart("密码重置：没有提交成功，请回复确认后我再提交。\n\n[本轮工具：reset_password；已生成待审批 x1:approval]")])

    response = asyncio.run(make_agent(reply, streaming=False).handle(
        Request("忘记密码了", conversation_state={}, principal=_alice(), allowed_writes=frozenset())))
    assert response.content == "密码重置：没有提交成功，请回复确认后我再提交。"
