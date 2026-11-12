"""Jev 在意图识别同一请求里给出的两项判断：要不要预取知识库、用户这一轮要求了哪些写操作。"""
import asyncio

import pytest

from agents.agent_orchestrator import AgentOrchestrator
from agents.intent.recognizer import IntentRecognizer
from agents.intent.taxonomy import WRITE_ACTIONS, IntentCategory, jev_questions
from agents.types import Request
from core.jev import JevResponse
from tooling.tool_manager import ToolResult


class FakeJev:
    def __init__(self, intent="vpn_issue", prob=.9, **nouls):
        self.intent, self.prob, self.nouls = intent, prob, nouls

    async def ask(self, state, questions):
        answers = {"intent": {"choice": self.intent, "probabilities": {self.intent: self.prob}},
                   "urgency": {"choice": "normal", "probabilities": {"normal": .9}}}
        answers.update({k: {"type": "noul", "noul": v} for k, v in self.nouls.items()})
        return JevResponse(answers=answers)


def recognize(jev, message="VPN 报错 VPN-691，帮我建单", llm=None):
    recognizer = IntentRecognizer("test", model="test", jev=jev)
    if llm:
        recognizer._llm = llm
    return asyncio.run(recognizer.recognize(message))


def test_questions_cover_every_write_action_and_knowledge():
    questions = jev_questions()
    assert questions["needs_sop"]["type"] == "noul"
    assert {f"wants_{tool}" for tool in WRITE_ACTIONS} <= set(questions)


def test_requested_actions_combine_jev_answers_with_write_intents():
    result = recognize(FakeJev(wants_create_ticket=.8, wants_request_access=.3, needs_sop=.45))
    assert result.requested_actions == frozenset({"create_ticket"}) and result.needs_knowledge is True
    permission = recognize(FakeJev(intent="permission_request", needs_sop=.1), "申请 GitLab 只读权限")
    assert permission.requested_actions == frozenset({"request_access"}) and permission.needs_knowledge is False


def test_jev_signals_survive_when_the_intent_goes_to_the_llm():
    async def llm(message, history):
        return {"intent": IntentCategory.TICKET_CREATE, "confidence": .9, "entities": {}, "_llm_used": True, "_source": "llm_tool_use"}

    result = recognize(FakeJev(prob=.3, needs_sop=.9), llm=llm)
    assert result.recognition_source == "llm_tool_use"
    assert result.requested_actions == frozenset({"create_ticket"}) and result.needs_knowledge is True


def test_without_jev_nothing_is_gated_or_prefetched():
    async def llm(message, history):
        return {"intent": IntentCategory.VPN_ISSUE, "confidence": .9, "entities": {}, "_llm_used": True, "_source": "llm_tool_use"}

    result = recognize(None, llm=llm)
    assert result.requested_actions is None and result.needs_knowledge is None


@pytest.mark.parametrize("allowed,succeeds", [(None, True), (frozenset({"create_ticket"}), True), (frozenset(), False)])
def test_unrequested_write_is_refused_before_it_runs(allowed, succeeds):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart

    from core.identity import Principal
    from evaluation.fake_model import scripted_model

    async def reply(messages, info):
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("create_ticket", {"title": "VPN", "description": "诊断"})])
        return ModelResponse(parts=[TextPart("好的。")])

    agent = AgentOrchestrator("sk-local").agent
    agent._llm = scripted_model(reply)
    alice = Principal("dev:alice", "alice", "engineering", frozenset({"employee"}), "1001")
    response = asyncio.run(agent.handle(Request("VPN 怎么办", conversation_state={}, principal=alice, allowed_writes=allowed)))
    trace = response.tool_traces[0]
    assert trace["success"] is succeeds
    if not succeeds:
        assert "没有要求" in trace["error"]


def test_chat_turn_prefetches_knowledge_only_when_jev_says_so(monkeypatch):
    from agents.intent.recognizer import IntentResult
    from api import state
    from api.routes import chat as chat_routes
    from api.schemas import ChatRequest
    from core.identity import Principal

    hit = {"title": "VPN-691认证排障", "content": "检查账号锁定。", "chunk": 0, "document_id": "54f69075" + "0" * 56, "updated_at": "2026-10-28T00:00:00Z"}

    class Tools:
        calls = []

        async def search(self, name, query, top_k, department):
            self.calls.append((query, top_k, department))
            return ToolResult(True, [hit], name)

    class Orchestrator:
        def __init__(self, needs):
            self.needs, self.seen = needs, None

        async def recognize_intent(self, message, history=None, previous_intent_group=None):
            return IntentResult(IntentCategory.VPN_ISSUE, .9, "network", needs_knowledge=self.needs, requested_actions=frozenset())

        async def run(self, req):
            self.seen = req
            return req

    alice = Principal("dev:alice", "alice", "engineering", frozenset({"employee"}), "1001")
    for needs, expected in ((True, [{"title": "VPN-691认证排障", "content": "检查账号锁定。", "updated_at": "2026-10-28", "citation": "VPN-691认证排障 · 54f69075:0"}]), (False, [])):
        orchestrator, tools = Orchestrator(needs), Tools()
        for name, value in {"orchestrator": orchestrator, "memory": None, "tool_manager": tools}.items():
            monkeypatch.setattr(state, name, value)
        asyncio.run(chat_routes._run_turn(ChatRequest(message="VPN 报 691"), alice, "r", "c", chat_routes._Stages()))
        assert tools.calls[-1] == ("VPN 报 691", 3, "engineering")
        assert orchestrator.seen.knowledge == expected and orchestrator.seen.allowed_writes == frozenset()
