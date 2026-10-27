"""Offline tests for the PydanticAI runtime: tool loop, guards, structured outputs; no network calls."""
import asyncio

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage

from agents.agent_orchestrator import AgentCallError, AgentOrchestrator, AgentType, Request
from core.intent_recognizer import IntentCategory, IntentRecognizer
from gateway.anthropic_to_openai import convert_tool_choice
from tooling.tool_manager import MCPToolManager


def _agent(agent_type, respond):
    agent = AgentOrchestrator("sk-local")._pool[agent_type][0]
    agent._llm = FunctionModel(respond)
    return agent


def _tool_returns(messages):
    return [part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)]


def test_tool_loop_records_trace_usage_and_whitelist_prompt():
    seen = {}

    async def respond(messages, info):
        seen["instructions"] = info.instructions
        seen["tools"] = sorted(tool.name for tool in info.function_tools)
        returns = _tool_returns(messages)
        if not returns:
            return ModelResponse(parts=[ToolCallPart("lookup_error_code", {"error_code": "vpn-691"}, tool_call_id="c1")], usage=RequestUsage(input_tokens=10, output_tokens=4))
        seen["tool_content"] = returns[-1].content
        return ModelResponse(parts=[TextPart("VPN-691 表示身份认证失败。")], usage=RequestUsage(input_tokens=20, output_tokens=6))

    async def run():
        result = await _agent(AgentType.NETWORK_SOFTWARE, respond).handle(Request("VPN 报错 VPN-691"))
        assert result.success and result.status == "success"
        assert result.tools_used == ["lookup_error_code"]
        assert result.tool_traces[0]["tool_use_id"] == "c1" and result.tool_traces[0]["success"] is True
        assert result.token_usage == {"input_tokens": 30, "output_tokens": 10}
        assert seen["tool_content"]["meaning"] == "VPN 身份认证或连接失败"
        assert "lookup_error_code" in seen["tools"] and "reset_password" not in seen["tools"]
        assert "工具白名单" in seen["instructions"] and "lookup_error_code" in seen["instructions"]

    asyncio.run(run())


def test_schema_violation_is_returned_to_model_without_running_tool():
    async def respond(messages, info):
        returns = _tool_returns(messages)
        if not returns:
            return ModelResponse(parts=[ToolCallPart("lookup_error_code", {"code": "x"})])
        assert returns[-1].content["success"] is False
        return ModelResponse(parts=[TextPart("请提供错误码。")])

    async def run():
        result = await _agent(AgentType.NETWORK_SOFTWARE, respond).handle(Request("VPN 报错"))
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
        result = await _agent(AgentType.ACCESS, respond).handle(req)
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
        result = await _agent(AgentType.ACCESS, respond).handle(req)
        assert result.tools_used == ["reset_password"] and commits == ["reset_password"]
        assert sorted(trace["success"] for trace in result.tool_traces) == [False, True]
        assert all(trace["retry_safe"] is False for trace in result.tool_traces)

    asyncio.run(run())


def test_tool_round_limit_raises_agent_call_error_with_evidence():
    async def respond(messages, info):
        return ModelResponse(parts=[ToolCallPart("check_service_status", {"service": "vpn"})])

    async def run():
        agent = _agent(AgentType.NETWORK_SOFTWARE, respond)
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
        result = await _agent(AgentType.TRIAGE, respond).handle(Request("你是谁"))
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


def test_gateway_forwards_tool_choice():
    assert convert_tool_choice({"type": "any"}) == "required"
    assert convert_tool_choice({"type": "tool", "name": "classify_it_request"}) == {"type": "function", "function": {"name": "classify_it_request"}}
    assert convert_tool_choice({"type": "none"}) == "none"
    assert convert_tool_choice(None) == "auto" and convert_tool_choice({"type": "auto"}) == "auto"
