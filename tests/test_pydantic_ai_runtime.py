"""Offline tests for the PydanticAI runtime: tool loop, guards, structured outputs; no network calls."""
import asyncio

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from agents.agent_orchestrator import AgentOrchestrator
from agents.intent.recognizer import IntentRecognizer
from agents.intent.taxonomy import IntentCategory
from agents.types import AgentCallError, Request
from tooling.tool_manager import MCPToolManager
from tests.conftest import with_read_tool


def _agent(respond):
    agent = with_read_tool(AgentOrchestrator("sk-local")).agent
    agent._llm = FunctionModel(respond)
    return agent


def _tool_returns(messages):
    return [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]


def test_tool_loop_records_trace_usage_and_lean_prompt():
    seen = {}

    async def respond(messages, info):
        seen["instructions"] = info.instructions
        seen["tools"] = sorted(tool.name for tool in info.function_tools)
        returns = _tool_returns(messages)
        if not returns:
            return ModelResponse(parts=[ToolCallPart("diagnose", {"code": "vpn-691"}, tool_call_id="c1")], usage=RequestUsage(input_tokens=10, output_tokens=4))
        seen["tool_content"] = returns[-1].content
        return ModelResponse(parts=[TextPart("VPN-691 表示身份认证失败。")], usage=RequestUsage(input_tokens=20, output_tokens=6))

    async def run():
        result = await _agent(respond).handle(Request("VPN 报错 VPN-691"))
        assert result.success and result.status == "success"
        assert result.tools_used == ["diagnose"]
        assert result.tool_traces[0]["tool_use_id"] == "c1" and result.tool_traces[0]["success"] is True
        assert result.token_usage == {"input_tokens": 30, "output_tokens": 10, "cache_read_tokens": 0}
        assert seen["tool_content"]["meaning"] == "测试诊断结果"
        assert {"diagnose", "reset_password", "create_ticket"} <= set(seen["tools"])
        # 工具清单只随工具定义下发，不在 system prompt 里重复。
        assert "规则：" in seen["instructions"] and "diagnose" not in seen["instructions"]

    asyncio.run(run())


def test_schema_violation_is_returned_to_model_without_running_tool():
    async def respond(messages, info):
        returns = _tool_returns(messages)
        if not returns:
            return ModelResponse(parts=[ToolCallPart("diagnose", {"error_code": "x"})])
        assert returns[-1].content["success"] is False
        return ModelResponse(parts=[TextPart("请提供错误码。")])

    async def run():
        result = await _agent(respond).handle(Request("VPN 报错"))
        assert result.success and result.tools_used == []
        assert "JSON Schema" in result.tool_traces[0]["error"]

    asyncio.run(run())


def test_write_tool_blocked_while_slots_missing():
    async def respond(messages, info):
        if not _tool_returns(messages):
            return ModelResponse(parts=[ToolCallPart("reset_password", {"employee_id": "E1001"})])
        return ModelResponse(parts=[TextPart("请先提供员工号。")])

    async def run():
        req = Request("重置密码", conversation_state={"missing_fields": ["employee_id"]})
        result = await _agent(respond).handle(req)
        assert result.tools_used == [] and req.tool_attempts == []
        assert "缺少必要信息" in result.tool_traces[0]["error"]

    asyncio.run(run())


def test_duplicate_write_tool_call_runs_once():
    commits = []

    async def respond(messages, info):
        if not _tool_returns(messages):
            call = {"employee_id": "E1001"}
            return ModelResponse(parts=[ToolCallPart("reset_password", call, tool_call_id="a"), ToolCallPart("reset_password", call, tool_call_id="b")])
        return ModelResponse(parts=[TextPart("已生成待审批请求。")])

    async def before_unsafe_tool(name):
        commits.append(name)

    async def run():
        req = Request("重置密码 员工号 E1001", before_unsafe_tool=before_unsafe_tool)
        result = await _agent(respond).handle(req)
        assert result.tools_used == ["reset_password"] and commits == ["reset_password"]
        assert sorted(trace["success"] for trace in result.tool_traces) == [False, True]
        assert all(trace["retry_safe"] is False for trace in result.tool_traces)

    asyncio.run(run())


def test_tool_round_limit_raises_agent_call_error_with_evidence():
    async def respond(messages, info):
        return ModelResponse(parts=[ToolCallPart("diagnose", {"code": "vpn"})])

    async def run():
        agent = _agent(respond)
        try:
            await agent._call_llm(Request("VPN 状态"))
        except AgentCallError as exc:
            assert "最大轮数" in str(exc) and exc.tools_used
        else:
            raise AssertionError("expected AgentCallError")
        result = await agent.handle(Request("VPN 状态"))
        assert result.status == "failed" and result.error_code == "agent_failed"

    asyncio.run(run())


def test_length_finish_reason_marks_partial():
    async def respond(messages, info):
        return ModelResponse(parts=[TextPart("回答被截断")], finish_reason="length")

    async def run():
        result = await _agent(respond).handle(Request("你是谁"))
        assert result.success and result.status == "partial"

    asyncio.run(run())


def test_intent_structured_output_via_classify_tool():
    async def respond(messages, info):
        assert [tool.name for tool in info.output_tools] == ["classify_it_request"] and not info.allow_text_output
        return ModelResponse(parts=[ToolCallPart("classify_it_request", {"intent": "permission_request", "confidence": 0.9, "urgency": "normal", "reasoning": "申请权限", "entities": {"system": ["GitLab"], "employee_id": [1001]}})])

    async def run():
        recognizer = IntentRecognizer("test", model="test")
        recognizer.llm = FunctionModel(respond)
        result = await recognizer.recognize("申请 GitLab 权限")
        assert result.intent == IntentCategory.PERMISSION_REQUEST
        assert result.recognition_source == "llm_tool_use" and result.llm_used
        assert "GitLab" in result.entities["system"] and "1001" in result.entities["employee_id"]

    asyncio.run(run())


def test_intent_invalid_structured_output_falls_back():
    async def respond(messages, info):
        return ModelResponse(parts=[ToolCallPart("classify_it_request", {"intent": "not-an-intent"})])

    async def run():
        recognizer = IntentRecognizer("test", model="test")
        recognizer.llm = FunctionModel(respond)
        result = await recognizer.recognize("VPN 连不上")
        assert result.recognition_source == "fallback_exception"
        assert result.intent == IntentCategory.VPN_ISSUE
        assert recognizer.metrics()["fallback_errors"] == 1

    asyncio.run(run())


def test_rerank_and_rewrite_use_structured_output():
    async def respond(messages, info):
        name = info.output_tools[0].name
        if name == "return_ranking":
            return ModelResponse(parts=[ToolCallPart(name, {"response": [2, 2, 0]})])
        return ModelResponse(parts=[ToolCallPart(name, {"response": ["VPN 认证失败", "  "]})])

    async def run():
        manager = MCPToolManager("sk-local")
        manager._llm = FunctionModel(respond)
        ranked, reranked = await manager._rerank_with_status("vpn", ["a", "b", "c"], 2)
        assert reranked and ranked == ["c", "a"]
        assert await manager.rewrite_query("VPN 排障") == ["VPN 排障", "VPN 认证失败"]

    asyncio.run(run())


def test_rerank_invalid_index_falls_back_to_original_order():
    async def respond(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"response": [9]})])

    async def run():
        manager = MCPToolManager("sk-local")
        manager._llm = FunctionModel(respond)
        assert await manager._rerank_with_status("vpn", ["a", "b", "c"], 2) == (["a", "b"], False)

    asyncio.run(run())



def test_thinking_type_reaches_upstream_request(monkeypatch):
    import json

    import httpx2
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from core.llm import _thinking_settings, complete_text

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
        })

    monkeypatch.setenv("OPENAI_THINKING_TYPE", "disabled")
    provider = OpenAIProvider(base_url="http://upstream.test/v1", api_key="k", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    model = OpenAIChatModel("m", provider=provider, settings=_thinking_settings())
    assert asyncio.run(complete_text(model, "hi", max_tokens=8)).text == "ok"
    assert seen["body"]["thinking"] == {"type": "disabled"}


def _search_manager(rows):
    from tooling.tool_manager import Tool

    manager = MCPToolManager("sk-local")
    calls = []

    async def knowledge(params, ctx=None):
        calls.append(params)
        return rows

    manager.register(Tool("knowledge_search", "kb", knowledge, {"type": "object", "properties": {}}))
    return manager, calls


def test_search_defaults_to_single_hybrid_query_without_llm(monkeypatch):
    monkeypatch.delenv("IT_COPILOT_RAG_REWRITE", raising=False)
    manager, calls = _search_manager([{"title": "VPN", "content": "证书"}])

    async def no_llm(messages, info):
        raise AssertionError("default search must not call the model")

    manager._llm = FunctionModel(no_llm)
    result = asyncio.run(manager.search("knowledge_search", "VPN 连不上", top_k=3, department="研发"))
    assert result.success and not result.reranked and result.data[0]["title"] == "VPN"
    assert calls == [{"query": "VPN 连不上", "top_k": 3, "department": "研发", "mode": "hybrid"}]
    assert asyncio.run(_search_manager([])[0].search("knowledge_search", "无结果")).outcome == "empty"
    assert not asyncio.run(manager.search("knowledge_search", "", top_k=3)).success


def test_search_rewrite_alone_merges_sub_queries_without_reranking(monkeypatch):
    monkeypatch.setenv("IT_COPILOT_RAG_REWRITE", "true")
    monkeypatch.delenv("IT_COPILOT_RAG_RERANK", raising=False)
    manager, calls = _search_manager([{"title": "VPN", "content": "证书"}])
    seen = []

    async def respond(messages, info):
        seen.append(info.output_tools[0].name)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"response": ["VPN 证书过期"]})])

    manager._llm = FunctionModel(respond)
    result = asyncio.run(manager.search("knowledge_search", "VPN 连不上", top_k=3))
    assert result.success and not result.reranked and seen == ["return_sub_queries"]
    assert sorted(c["query"] for c in calls) == ["VPN 证书过期", "VPN 连不上"]


def test_search_rerank_alone_recalls_candidates_and_reorders(monkeypatch):
    monkeypatch.delenv("IT_COPILOT_RAG_REWRITE", raising=False)
    monkeypatch.setenv("IT_COPILOT_RAG_RERANK", "true")
    rows = [{"title": f"doc{i}", "content": f"片段{i}"} for i in range(6)]
    manager, calls = _search_manager(rows)
    seen = []

    async def respond(messages, info):
        seen.append(info.output_tools[0].name)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"response": [5, 4]})])

    manager._llm = FunctionModel(respond)
    result = asyncio.run(manager.search("knowledge_search", "VPN 连不上", top_k=3))
    assert seen == ["return_ranking"]                              # no rewrite call
    assert calls == [{"query": "VPN 连不上", "top_k": 20, "department": None, "mode": "hybrid"}]
    assert result.reranked and [r["title"] for r in result.data] == ["doc5", "doc4", "doc0"]
