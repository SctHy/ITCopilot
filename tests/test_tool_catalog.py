"""工具与提示词的“体积预算”和契约：Schema 合法、描述精简、低价值工具已移除、检索结果精简。

预算是回归护栏：有人往 system prompt 或工具描述里塞长段说明时，这里会先失败。
"""
import asyncio
import json

import pytest
from jsonschema import validators

from agents.agent_orchestrator import AgentOrchestrator
from agents.prompting import AGENT_RULES
from agents.tools import build_shared_rag_tools
from agents.types import AgentType, Request
from tooling.tool_manager import ToolResult


@pytest.fixture(scope="module")
def orchestrator():
    orch = AgentOrchestrator("sk-local")
    orch.set_shared_tools(build_shared_rag_tools(object()))
    return orch


def all_specs(orch):
    return {spec.name: spec for agents in orch._pool.values() for agent in agents for spec in agent.get_tools().values()}


def test_every_tool_schema_is_valid_json_schema_and_closed(orchestrator):
    for spec in all_specs(orchestrator).values():
        validators.validator_for(spec.input_schema).check_schema(spec.input_schema)
        assert spec.input_schema["type"] == "object" and spec.input_schema.get("additionalProperties") is False, spec.name
        assert set(spec.input_schema.get("required", [])) <= set(spec.input_schema["properties"]), spec.name


def test_low_value_tools_are_gone_and_triage_needs_no_tools(orchestrator):
    names = set(all_specs(orchestrator))
    assert not names & {"inspect_request_context", "suggest_required_fields", "build_diagnostic_plan"}
    assert set(orchestrator._pool[AgentType.TRIAGE][0].get_tools()) == {"search_knowledge_base"}
    assert set(orchestrator._generalist.get_tools()) == {
        "search_knowledge_base", "lookup_asset", "lookup_error_code", "check_service_status", "query_ticket",
        "create_ticket", "reset_password", "unlock_account", "request_access"}


def test_write_tools_are_not_retry_safe_and_read_tools_are(orchestrator):
    writes = {"create_ticket", "reset_password", "unlock_account", "request_access"}
    for name, spec in all_specs(orchestrator).items():
        assert spec.retry_safe is (name not in writes), name


def test_prompt_and_tool_budgets(orchestrator):
    for agents in orchestrator._pool.values():
        for agent in agents:
            prompt = agent._build_system_prompt(Request("x"))
            # 300 字以内：每条规则都对应评测里出现过的失败（先查 SOP、不主动提交、引用时重新检索等）。
            assert len(prompt) <= 300 and prompt.endswith(AGENT_RULES), agent.agent_type
    specs = orchestrator._generalist.get_tools().values()
    assert all(len(spec.description) <= 80 for spec in specs)
    payload = [{"name": s.name, "description": s.description, "parameters": s.input_schema} for s in specs]
    assert len(json.dumps(payload, ensure_ascii=False)) <= 2600


class FakeManager:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def search(self, name, query, top_k, department):
        self.calls.append((name, query, top_k, department))
        return self.result


HIT = {"title": "VPN-809 UDP端口排障", "content": "先检查 UDP 500 和 4500。", "score": .03, "vector_score": .69, "bm25_score": 14.3,
       "chunk": 2, "chunk_id": "9bf0225c" + "a" * 56 + ":2", "document_id": "9bf0225c" + "a" * 56, "version": "b" * 64,
       "source": "sop/vpn809", "department": "engineering", "updated_at": "2026-10-28T00:00:00Z"}


def run_search(result, args, department="engineering"):
    manager = FakeManager(result)
    spec = build_shared_rag_tools(manager)["search_knowledge_base"]
    out = asyncio.run(spec.handler(Request("VPN-809 怎么处理", department=department), args))
    return out, manager.calls


def test_search_tool_returns_slim_hits_and_passes_department():
    out, calls = run_search(ToolResult(True, [HIT], "knowledge_search"), {"query": "VPN-809", "top_k": 3})
    assert calls == [("knowledge_search", "VPN-809", 3, "engineering")]
    assert out == {"success": True, "query": "VPN-809", "error": "",
                   "results": [{"title": "VPN-809 UDP端口排障", "content": "先检查 UDP 500 和 4500。", "updated_at": "2026-10-28",
                                "citation": "VPN-809 UDP端口排障 · 9bf0225c:2"}]}


def test_search_tool_failure_and_empty_query():
    out, _ = run_search(ToolResult(False, [], "knowledge_search", error="检索无可用结果", outcome="empty"), {"query": "x"})
    assert out["success"] is False and out["results"] == [] and out["error"] == "检索无可用结果"
    blank, calls = run_search(ToolResult(True, [HIT], "knowledge_search"), {"query": "  "})
    assert blank["success"] is True and calls[0][1] == "VPN-809 怎么处理"  # 空查询退回用户原话
