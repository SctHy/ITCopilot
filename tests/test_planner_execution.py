"""Planner dependency evidence, failure blocking and token budget stop."""
import asyncio


def test_planner_passes_evidence_and_blocks_failed_dependencies():
    from agents.agent_orchestrator import AgentOrchestrator, AgentResponse, Request
    from core.planner import ExecutionPlan, PlanStep

    async def run():
        o = AgentOrchestrator("test")
        plan = ExecutionPlan(
            "p",
            "goal",
            [
                PlanStep("network", "diagnose", "network_software", []),
                PlanStep("ticket", "record", "ticket", ["network"]),
            ],
        )
        calls = []

        async def execute(req, kind):
            calls.append(req)
            if kind.value == "ticket":
                assert (
                    req.step_outputs["network"]["evidence"][0]["output"]["error_code"] == "VPN-691"
                )
            return AgentResponse(
                kind, "diagnosis", True, tool_traces=[{"output": {"error_code": "VPN-691"}}]
            )

        o._execute = execute
        assert (await o._run_plan(Request("x"), plan)).status == "success"
        assert len(calls) == 2

        async def failure(req, kind):
            assert kind.value == "network_software"
            return AgentResponse(kind, "unavailable", False, status="failed")

        o._execute = failure
        result = await o._run_plan(Request("x"), plan)
        assert result.status == "failed"
        assert any(e.get("status") == "blocked" for e in result.trace_events)

    asyncio.run(run())


def test_budget_stop_preserves_spent_tokens_and_tool_evidence():
    from agents.agent_orchestrator import AgentOrchestrator, AgentResponse, Request
    from core.planner import ExecutionPlan, PlanStep

    async def run():
        o = AgentOrchestrator("test")

        async def execute(req, kind):
            return AgentResponse(
                kind,
                "evidence",
                True,
                tools_used=["lookup_error_code"],
                tool_traces=[{"tool_name": "lookup_error_code", "success": True}],
                token_usage={"input_tokens": 11},
            )

        o._execute = execute
        plan = ExecutionPlan(
            "p",
            "goal",
            [
                PlanStep("a", "diagnose", "network_software", []),
                PlanStep("b", "record", "ticket", ["a"]),
            ],
            token_budget=10,
        )
        result = await o._run_plan(Request("x"), plan)
        assert result.error_code == "plan_budget_exceeded"
        assert result.token_usage["input_tokens"] == 11
        assert result.tools_used == ["lookup_error_code"] and result.tool_traces

    asyncio.run(run())


def test_plan_budget_default_and_override(monkeypatch):
    from core.planner import build_plan

    assert build_plan("VPN 和电脑同时坏了", "p").token_budget == 20000           # 10k per step
    monkeypatch.setenv("IT_COPILOT_PLAN_TOKENS_PER_STEP", "15000")
    assert build_plan("VPN 和电脑同时坏了", "p").token_budget == 30000
    monkeypatch.setenv("IT_COPILOT_PLAN_TOKEN_BUDGET", "45000")
    assert build_plan("VPN 和电脑同时坏了", "p").token_budget == 45000


def test_diagnostic_agents_can_file_tickets_but_triage_cannot():
    from agents.agent_orchestrator import AgentOrchestrator, AgentType

    orchestrator = AgentOrchestrator("test")
    has_ticket = {t: "create_ticket" in agents[0].get_tools() for t, agents in orchestrator._pool.items()}
    assert has_ticket == {AgentType.TRIAGE: False, AgentType.ENDPOINT: True, AgentType.ACCESS: True,
                          AgentType.NETWORK_SOFTWARE: True, AgentType.TICKET: True, AgentType.GENERALIST: True}


def _scripted_agent_model(seen_tools, calls):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    async def reply(messages, info):
        seen_tools.append({t.name for t in info.function_tools})
        done = sum(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
        if done < len(calls):
            return ModelResponse(parts=[ToolCallPart(*calls[done])])
        return ModelResponse(parts=[TextPart("完成")])
    return FunctionModel(reply)


def test_plan_steps_hide_ticket_tool_when_a_ticket_step_owns_it():
    from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request
    from core.planner import ExecutionPlan, PlanStep

    async def run():
        orchestrator = AgentOrchestrator("test")
        seen = []
        orchestrator._pool[AgentType.ENDPOINT][0]._llm = _scripted_agent_model(seen, [])
        plan = ExecutionPlan("p", "goal", [PlanStep("endpoint", "diagnose", "endpoint", []), PlanStep("ticket", "record", "ticket", ["endpoint"])])
        orchestrator._pool[AgentType.TICKET][0]._llm = _scripted_agent_model([], [])
        await orchestrator._run_plan(Request("电脑黑屏，请建工单"), plan)
        assert seen and all("create_ticket" not in tools for tools in seen)
        assert all("lookup_asset" in tools for tools in seen)

    asyncio.run(run())


def test_diagnose_then_ticket_needs_four_rounds_and_is_not_cut_off():
    from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request

    async def run():
        orchestrator = AgentOrchestrator("test")
        agent = orchestrator._pool[AgentType.ENDPOINT][0]
        agent._llm = _scripted_agent_model([], [
            ("lookup_asset", {"asset_id": "DEV-002"}),
            ("build_diagnostic_plan", {"environment": "laptop", "reproduced": True}),
            ("create_ticket", {"title": "黑屏", "description": "已诊断"}),
        ])
        from core.identity import Principal
        alice = Principal("dev:alice", "alice", "engineering", frozenset({"employee"}), "1001")
        response = await agent.handle(Request("DEV-002 黑屏，诊断后建工单", conversation_state={}, principal=alice))
        assert response.status == "success" and response.tools_used == ["lookup_asset", "build_diagnostic_plan", "create_ticket"]

    asyncio.run(run())
