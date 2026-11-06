"""有区分度的难例：边界、对抗输入、故障注入、跨租户隔离与随机输入下的不变量。

每组都针对一个真实出过或容易出的错：编号误识别、无据声称、历史结构破坏、缓存串部门、
并行预取把整轮拖垮、流式草稿与最终答案不一致等。随机用例用固定种子，结果可复现。
"""
import asyncio
import random
import string

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from agents.agent_orchestrator import AgentOrchestrator
from agents.conversation_state import with_login_employee
from agents.intent.entities import extract_entities
from agents.prompting import WRITE_CLAIM, history_messages
from agents.types import Request
from evaluation.fake_model import scripted_model
from memory.conversation_memory import MemoryContext, Message, MsgRole
from tooling.tool_manager import MCPToolManager, Tool

# ── 编号抽取：对抗输入 ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("message,employee,ticket", [
    ("工号E1001，工单INC-2002", ["E1001"], ["INC-2002"]),                      # 中文紧贴编号
    ("请看 https://wiki/E1001 和 mail INC-1001@corp", ["E1001"], ["INC-1001"]),  # 出现在 URL/邮箱里也算一次
    ("型号 XE1001、IDE1001、E1001X 都不是工号", [], []),                         # 字母粘连不算
    ("E123 太短，E1234567 太长，E12345 正好", ["E12345"], []),                    # 长度边界 4–6 位
    ("报错E1003；工号E1004", ["E1004"], []),                                     # 只排除紧跟错误码提示的那个
    ("错误码是 E1003 ，另外工号 E1005", ["E1005"], []),                          # “错误码是”后的不是工号
    ("INCIDENT-2024、Including、inc 都不是单号；inc-77 太短", [], []),
    ("单号 INC-2024-0001 和 INC20240002，重复 INC-2024-0001", [], ["INC-2024-0001", "INC20240002"]),
])
def test_bare_id_extraction_edges(message, employee, ticket):
    entities = extract_entities(message)
    assert entities["employee_id"] == employee and entities["ticket_id"] == ticket


# ── 无据声称识别：正例与负例 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "按你的要求，我已创建工单待审批。", "已经为你提交密码重置申请", "已帮你生成权限申请，等待审批。", "已发起解锁请求",
])
def test_write_claim_detects_completed_claims(text):
    assert WRITE_CLAIM.search(text)


@pytest.mark.parametrize("text", [
    "需要我为你创建工单吗？", "确认后我会提交申请。", "工单审批通过后才会生成单号。", "请先提供工号，我再提交待审批申请。",
    "已确认工号 1002，请确认是否发起重置。",                                      # “已确认”不是“已提交”
])
def test_write_claim_ignores_offers_and_plans(text):
    assert not WRITE_CLAIM.search(text)


# ── 历史结构：随机输入下的不变量 ─────────────────────────────────────────────────

def test_history_messages_invariants_on_random_turn_sequences():
    rng = random.Random(20261106)
    for _ in range(500):
        turns = [{"role": rng.choice(["user", "assistant", "system", "tool"]),
                  "content": rng.choice(["", " ", "".join(rng.choices(string.ascii_letters + "中文", k=rng.randint(1, 8)))])}
                 for _ in range(rng.randint(0, 12))]
        messages = history_messages(turns)
        kinds = [type(m) for m in messages]
        # 用户/助手严格交替，以用户开头、以助手结尾（下一条是本轮用户消息）。
        assert all(a is not b for a, b in zip(kinds, kinds[1:]))
        assert not messages or (kinds[0] is ModelRequest and kinds[-1] is ModelResponse)
        # 不凭空产生内容：输出里的每段文字都来自输入里的非空用户/助手消息。
        sources = {t["content"] for t in turns if t["role"] in ("user", "assistant") and t["content"]}
        for m in messages:
            assert all(piece in sources for piece in m.parts[0].content.split("\n"))


def test_history_truncation_boundary_is_exact_and_deterministic():
    def turns(n):
        ctx = MemoryContext([Message(MsgRole.USER, "q"), Message(MsgRole.ASSISTANT, "字" * n)], [], {}, "")
        return ctx.history_turns(assistant_chars=600)[1]["content"]

    assert turns(600) == "字" * 600                                  # 恰好等于上限不截断
    assert turns(601) == "字" * 600 + "…（已截断）" == turns(601)      # 超出一个字就截断，且每次一样


# ── 槽位优先级：本轮 > 历史 > 登录身份，身份敏感操作不默认 ───────────────────────────

@pytest.mark.parametrize("current,previous,intent,expected", [
    ({}, {}, "ticket_create", ["1001"]),
    ({"employee_id": ["E2002"]}, {}, "ticket_create", ["E2002"]),
    ({}, {"entities": {"employee_id": ["E3003"]}}, "permission_request", None),   # 历史里有，交给 merge_state
    ({}, {}, "password_reset", None),
    ({}, {}, "account_unlock", None),
])
def test_login_employee_default_priority(current, previous, intent, expected):
    assert with_login_employee(current, previous, "1001", intent).get("employee_id") == expected
    assert with_login_employee(current, previous, None, intent).get("employee_id") == current.get("employee_id")


# ── 缓存隔离：同一查询、不同部门不能共用缓存结果 ──────────────────────────────────

def test_tool_cache_never_leaks_results_across_departments():
    calls = []

    async def handler(params, context=None):
        calls.append(params["department"])
        return [f"{params['department']} 专属文档"]

    manager = MCPToolManager("sk-local")
    manager.register(Tool(name="knowledge_search", description="kb", handler=handler, cache_ttl=60,
                          schema={"type": "object", "properties": {"query": {"type": "string"}, "department": {"type": "string"}}}))

    async def run():
        a1 = await manager.call("knowledge_search", {"query": "VPN", "department": "财务"})
        b1 = await manager.call("knowledge_search", {"query": "VPN", "department": "研发"})
        a2 = await manager.call("knowledge_search", {"query": "VPN", "department": "财务"})
        return a1, b1, a2

    a1, b1, a2 = asyncio.run(run())
    assert a1.data == ["财务 专属文档"] and b1.data == ["研发 专属文档"]
    assert a2.cached and a2.data == ["财务 专属文档"] and calls == ["财务", "研发"]
    a2.data.append("篡改")                                        # 调用方改动返回值不能污染缓存
    assert asyncio.run(manager.call("knowledge_search", {"query": "VPN", "department": "财务"})).data == ["财务 专属文档"]


# ── 故障注入：预取失败不能拖垮整轮 ───────────────────────────────────────────────

def test_chat_turn_survives_a_crashing_knowledge_prefetch(monkeypatch):
    from agents.intent.recognizer import IntentResult
    from agents.intent.taxonomy import IntentCategory
    from api import state
    from api.routes import chat as chat_routes
    from api.schemas import ChatRequest
    from core.identity import Principal

    class BrokenTools:
        async def search(self, *args, **kwargs):
            raise RuntimeError("vector store down")

    class Orchestrator:
        seen = None

        async def recognize_intent(self, message, history=None, previous_intent_group=None):
            return IntentResult(IntentCategory.VPN_ISSUE, .9, "network", needs_knowledge=True)

        async def run(self, req):
            Orchestrator.seen = req
            return req

    for name, value in {"orchestrator": Orchestrator(), "memory": None, "tool_manager": BrokenTools()}.items():
        monkeypatch.setattr(state, name, value)
    alice = Principal("dev:alice", "alice", "engineering", frozenset({"employee"}), "1001")
    asyncio.run(chat_routes._run_turn(ChatRequest(message="VPN 报 691"), alice, "r", "c", chat_routes._Stages()))
    assert Orchestrator.seen is not None and Orchestrator.seen.knowledge == []


# ── 流式：随机切分下，最后一次 reset 之后的增量拼起来恰好等于最终答案 ───────────────────

def test_streamed_deltas_after_last_reset_equal_final_answer_for_random_chunking(monkeypatch):
    rng = random.Random(7)
    for trial in range(20):
        preambles = ["先查一下错误码。", "再看看服务状态，" * rng.randint(1, 3)]
        final = "".join(rng.choices("VPN-691 认证失败，检查账号锁定与系统时间。", k=rng.randint(1, 120))) or "好"

        async def reply(messages, info, preambles=preambles, final=final):
            done = sum(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
            tools = ["lookup_error_code", "check_service_status"]
            if done < len(tools):
                return ModelResponse(parts=[TextPart(preambles[done]), ToolCallPart(tools[done], {"error_code": "VPN-691"} if done == 0 else {})])
            return ModelResponse(parts=[TextPart(final)])

        chunk = rng.randint(1, 9)
        monkeypatch.setattr("evaluation.fake_model.STREAM_CHUNK", chunk)
        events = []

        async def emit(event, data):
            events.append((event, data))

        agent = AgentOrchestrator("sk-local")._generalist
        agent._llm = scripted_model(reply)
        response = asyncio.run(agent.handle(Request("VPN 报 691", emit=emit, stream_answer=True)))
        names = [e for e, _ in events]
        assert names.count("answer_reset") == 2, (trial, names)
        last_reset = len(names) - 1 - names[::-1].index("answer_reset")
        # 最终答案会去掉首尾空白，流式增量不会。
        assert "".join(d["delta"] for e, d in events[last_reset:] if e == "answer_delta").strip() == response.content == final.strip()


# ── 写操作把关与无据纠正叠加：被拒的写操作不算证据，纠正只发生一次 ─────────────────────

def test_refused_write_does_not_count_as_evidence_and_correction_happens_once():
    calls = []

    async def reply(messages, info):
        calls.append(messages)
        last = [p for p in messages[-1].parts if isinstance(p, UserPromptPart)]
        if last and "[系统核对]" in last[0].content:
            return ModelResponse(parts=[TextPart("我已为你创建工单待审批。")])      # 纠正后仍然嘴硬
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("create_ticket", {"title": "VPN", "description": "诊断"})])
        return ModelResponse(parts=[TextPart("已为你创建工单待审批。")])

    agent = AgentOrchestrator("sk-local")._generalist
    agent._llm = scripted_model(reply)
    response = asyncio.run(agent.handle(Request("VPN 怎么办", conversation_state={}, allowed_writes=frozenset())))
    assert response.tool_traces[0]["success"] is False and "没有要求" in response.tool_traces[0]["error"]
    assert len(calls) == 3                                        # 工具一轮 + 回答一轮 + 纠正一轮，不会无限纠正


@pytest.mark.parametrize("message,device,errors", [
    ("VPN 报 VPN-691 和设备 DEV-023 黑屏", ["DEV-023"], ["VPN-691"]),   # 资产号不能被当成错误码
    ("型号 DEV-9 太短，不带标签不算", [], []),
    ("dev-1234 小写也认", ["DEV-1234"], []),
    ("报错 ABC-123", [], ["ABC-123"]),
])
def test_device_ids_are_not_mistaken_for_error_codes(message, device, errors):
    entities = extract_entities(message)
    assert entities["device_id"] == device and entities["error_code"] == errors
