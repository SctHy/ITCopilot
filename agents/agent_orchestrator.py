"""多 Agent 编排：意图 → 规划策略 → 执行与汇总。"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import replace
from typing import Any, Dict, List, Optional

from agents.base_agent import (
    AccessAgent,
    BaseAgent,
    EndpointAgent,
    GeneralistAgent,
    NetworkSoftwareAgent,
    TicketAgent,
    TriageAgent,
)
from agents.injection_guard import InjectionGuard
from agents.intent.recognizer import IntentRecognizer
from agents.intent.taxonomy import INTENT_GROUPS, IntentCategory
from agents.planner import STEP, LLMPlanner, StepStatus, build_plan
from agents.tools import (
    access_tools,
    build_shared_rag_tools,
    endpoint_tools,
    network_software_tools,
    ticket_tools,
    triage_tools,
)
from agents.types import (
    GROUP_AGENTS,
    AgentResponse,
    AgentType,
    OrchestratorResult,
    Request,
    RoutingDecision,
)
from core.env import env_float
from core.llm import build_model
from tooling.executor import ToolExecutor

logger = logging.getLogger(__name__)
MAX_PLAN_WAVE = 3


def _sum_usage(responses) -> Dict[str, int]:
    return {key: sum(r.token_usage.get(key, 0) for r in responses) for key in ("input_tokens", "output_tokens", "cache_read_tokens")}


AGENT_SPECS = (
    (AgentType.TRIAGE, TriageAgent, triage_tools),
    (AgentType.ENDPOINT, EndpointAgent, endpoint_tools),
    (AgentType.ACCESS, AccessAgent, access_tools),
    (AgentType.NETWORK_SOFTWARE, NetworkSoftwareAgent, network_software_tools),
    (AgentType.TICKET, TicketAgent, ticket_tools),
)


# generalist   一个 Agent、全部工具（默认；v3 分层评测成功率最高）
# bounded      LLM 规划器按内容拆步，专职 Agent 只用本领域工具并行执行（权限隔离）
# bounded_all  同 bounded，但每个专职 Agent 都持有全部工具（对照：拆步本身的价值）
# single       按意图路由到一个专职 Agent（对照基线）
PLANNING_MODES = ("generalist", "bounded", "bounded_all", "single")


class AgentOrchestrator:
    def __init__(self, api_key="", base_url=None, model="deepseek-chat", skill_manager=None, recognizer=None, rag_tool_manager=None, **kwargs):
        self._llm = build_model(api_key, base_url, model)
        self._model = model
        self._skill_manager = skill_manager
        self._planning_mode = None
        self._planner = LLMPlanner(self._llm) if os.getenv("IT_COPILOT_LLM_PLANNER", "true").lower() in {"1", "true", "yes"} else None
        self._intent_recognizer = recognizer or IntentRecognizer(api_key, base_url, model)
        self._recent_tool_traces = deque(maxlen=200)
        self.tool_executor = ToolExecutor(timeout_s=env_float("IT_COPILOT_TOOL_TIMEOUT_S", 10))
        # 工具分三层，每次变更都从这三层重新组装，保证下线的外部工具会真正从 Agent 移除。
        self._shared_tools = build_shared_rag_tools(rag_tool_manager) if rag_tool_manager else {}
        self._external_tools = {}
        self._local_tools: Dict[AgentType, Dict[str, Any]] = {}
        self._pool: Dict[AgentType, List[BaseAgent]] = {}
        for agent_type, cls, tool_factory in AGENT_SPECS:
            self._local_tools[agent_type] = tool_factory()
            agent = cls(self._llm, model, skill_manager)
            agent.agent_type = agent_type
            agent.executor = self.tool_executor
            self._pool[agent_type] = [agent]
        # 通用 Agent 也在池中：参与监控、降级冷却与 Triage 兜底，但意图路由不会选中它。
        self._all_tools = {name: spec for tools in self._local_tools.values() for name, spec in tools.items()}
        self._local_tools[AgentType.GENERALIST] = self._all_tools
        self._generalist = GeneralistAgent(self._llm, model, skill_manager)
        self._generalist.agent_type = AgentType.GENERALIST
        self._generalist.executor = self.tool_executor
        self._pool[AgentType.GENERALIST] = [self._generalist]
        # 所有 Agent 共用一个检测器（一个 HTTP 连接池），关停时由 aclose 释放。
        self.injection_guard = InjectionGuard.from_env()
        for agents in self._pool.values():
            for agent in agents:
                agent.injection_guard = self.injection_guard
        self.planning_mode = os.getenv("IT_COPILOT_PLANNING_MODE", "generalist")

    async def aclose(self) -> None:
        """释放外部客户端（注入检测与意图识别的 Jev 连接池）。"""
        if self.injection_guard:
            await self.injection_guard.aclose()
        await self._intent_recognizer.aclose()

    @property
    def planning_mode(self) -> str:
        return self._planning_mode

    @planning_mode.setter
    def planning_mode(self, mode: str) -> None:
        if mode not in PLANNING_MODES:
            raise ValueError(f"planning mode must be one of {PLANNING_MODES}")
        self._planning_mode = mode
        self._rebuild_tools()

    def agent_types(self) -> List[AgentType]:
        return list(self._pool)

    def _rebuild_tools(self) -> None:
        all_tools = self._planning_mode == "bounded_all"
        for agent_type, agents in self._pool.items():
            local = self._all_tools if all_tools and agent_type != AgentType.TRIAGE else self._local_tools[agent_type]
            for agent in agents:
                # 本地白名单工具优先于同名的共享/外部工具。
                agent.set_shared_tools({**self._shared_tools, **self._external_tools, **local})

    def set_shared_tools(self, tools):
        self._shared_tools = dict(tools or {})
        self._rebuild_tools()

    def set_external_tools(self, tools):
        """外部 MCP 工具集合整体替换（连接/断开/工具列表变化时由 MCP 客户端回调）。"""
        self._external_tools = dict(tools or {})
        self._rebuild_tools()

    async def recognize_intent(self, message, history=None, previous_intent_group=None):
        return await self._intent_recognizer.recognize(message, history=history, previous_intent_group=previous_intent_group)

    def _best_agent(self, agent_type: AgentType) -> Optional[BaseAgent]:
        agents = [a for a in self._pool.get(agent_type, []) if a.stats.available()]
        if agents:
            return max(agents, key=lambda a: a.stats.routing_score())
        return (self._pool.get(agent_type) or [None])[0]

    @staticmethod
    def _route(intent: Optional[IntentCategory]) -> AgentType:
        return GROUP_AGENTS.get(INTENT_GROUPS.get(intent, "general"), AgentType.TRIAGE)

    async def _execute(self, req: Request, agent_type: AgentType) -> AgentResponse:
        agent = self._best_agent(agent_type) or self._best_agent(AgentType.TRIAGE)
        if agent is None:
            return AgentResponse(AgentType.TRIAGE, "服务暂时不可用。", False, status="failed", error_code="agent_unavailable")
        response = await agent.handle(req)
        if response.error_code == "agent_busy" and agent.agent_type != AgentType.TRIAGE:
            # 专职 Agent 处于降级冷却：由 Triage 接住请求，而不是直接让用户重试。
            fallback = self._best_agent(AgentType.TRIAGE)
            if fallback is not None:
                return await fallback.handle(req)
        return response

    async def run(self, req: Request) -> OrchestratorResult:
        started = time.monotonic()
        if req.intent is None:
            ir = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent, req.intent_group, req.urgency, req.intent_confidence, req.entities = ir.intent, ir.intent_group, ir.urgency, ir.confidence, ir.entities
        if self.planning_mode == "generalist":
            await req.publish("routed", {"agent_types": ["generalist"], "reason": "one agent, all tools"})
            req.stream_answer = True
            response = await self._execute(req, AgentType.GENERALIST)
            result = OrchestratorResult(
                request_id=req.request_id, response=response.content, agent_type=response.agent_type, intent=req.intent,
                escalated=response.escalate or req.intent == IntentCategory.HUMAN_HANDOFF, latency_ms=(time.monotonic() - started) * 1000,
                agent_types=[response.agent_type], primary_agent=AgentType.GENERALIST, tools_used=response.tools_used,
                tool_traces=response.tool_traces, routing_reason="generalist: one agent, all tools",
                routing_confidence=req.intent_confidence, status=response.status, error_code=response.error_code, token_usage=response.token_usage,
            )
            self._recent_tool_traces.append(result.tool_traces)
            return result
        if self.planning_mode in {"bounded", "bounded_all"}:
            # 按内容规划，不只依赖意图类别；LLM 规划失败时退回关键词规则。
            plan = await self._planner.plan(req.message, req.request_id, req.history) if self._planner else build_plan(req.message, req.request_id)
            await req.publish("plan", plan.to_dict())
            return await self._run_plan(req, plan)

        main = self._route(req.intent)
        # “A 和 B” 式的网络/权限复合问题由 Triage 并行补充分流建议。
        supporting = []
        decision = RoutingDecision(main, supporting, f"intent={req.intent.value}; performance_score={self._best_agent(main).stats.routing_score():.3f}", req.intent_confidence)
        await req.publish("routed", {"agent_types": [t.value for t in decision.agent_types], "reason": decision.reason})
        if decision.multi_agent:
            return await self.run_parallel(req, decision)

        req.stream_answer = True
        response = await self._execute(req, main)
        result = OrchestratorResult(
            request_id=req.request_id, response=response.content, agent_type=response.agent_type, intent=req.intent,
            escalated=response.escalate or req.intent == IntentCategory.HUMAN_HANDOFF, latency_ms=(time.monotonic() - started) * 1000,
            agent_types=[response.agent_type], primary_agent=main, tools_used=response.tools_used, tool_traces=response.tool_traces,
            routing_reason=decision.reason, routing_confidence=decision.confidence, status=response.status,
            error_code=response.error_code, token_usage=response.token_usage,
        )
        self._recent_tool_traces.append(result.tool_traces)
        return result

    async def _run_plan(self, req: Request, plan) -> OrchestratorResult:
        """Execute bounded dependency waves; independent steps run concurrently."""
        started = time.monotonic()
        completed, step_rows, findings, responses = set(), [], [], []
        outputs, unsuccessful, statuses = {}, set(), {}

        def mark(step, status: StepStatus, **detail) -> Dict[str, Any]:
            statuses[step.step_id] = STEP.check(statuses.get(step.step_id, StepStatus.PENDING), status)
            event = "step_started" if status == StepStatus.RUNNING else "step_finished"
            step_rows.append({"event": event, "step_id": step.step_id, "status": str(status), **detail})
            if status != StepStatus.RUNNING:
                completed.add(step.step_id)
                if status != StepStatus.SUCCESS:
                    unsuccessful.add(step.step_id)
            return step_rows[-1]

        def failed(message: str, error_code: str) -> OrchestratorResult:
            return OrchestratorResult(
                request_id=req.request_id, response=message, agent_type=AgentType.TRIAGE, intent=req.intent, escalated=True,
                latency_ms=(time.monotonic() - started) * 1000, status="failed", error_code=error_code, plan=plan.to_dict(statuses),
                findings=findings, trace_events=step_rows,
                tools_used=list(dict.fromkeys(t for r in responses for t in r.tools_used)),
                tool_traces=[t for r in responses for t in r.tool_traces], token_usage=_sum_usage(responses),
            )

        while len(completed) < len(plan.steps) and len(step_rows) < plan.max_steps * 2:
            ready = [s for s in plan.steps if s.step_id not in completed and all(d in completed for d in s.depends_on)]
            if not ready:
                return failed("计划依赖无法满足，已停止执行并转人工。", "plan_dependency_cycle")
            blocked = [s for s in ready if any(d in unsuccessful for d in s.depends_on)]
            for step in blocked:
                mark(step, StepStatus.BLOCKED, error_code="dependency_failed")
            ready = [s for s in ready if s not in blocked]
            if not ready:
                continue
            used = _sum_usage(responses)
            # cache_read_tokens 是 input_tokens 的一部分，不能再加一遍。
            if used["input_tokens"] + used["output_tokens"] >= plan.token_budget:
                return failed("计划已用完轮次间 token 预算，停止后续步骤。", "plan_budget_exceeded")
            wave = ready[:MAX_PLAN_WAVE]
            for s in wave:
                mark(s, StepStatus.RUNNING, agent_type=s.agent_type)
                await req.publish("step_started", {"step_id": s.step_id, "agent_type": s.agent_type, "title": s.title})
            # 计划含工单步骤时由它汇总建单；诊断步骤不提供 create_ticket，避免一次请求生成多张工单。
            ticket_owned = frozenset({"create_ticket"}) if any(p.agent_type == "ticket" for p in plan.steps) else frozenset()
            raw = await asyncio.gather(*[self._execute(replace(req, message=f"[计划步骤: {s.title}]\n{req.message}",
                step_outputs={d: outputs[d] for d in s.depends_on},
                hidden_tools=req.hidden_tools | (ticket_owned if s.agent_type != "ticket" else frozenset())), AgentType(s.agent_type)) for s in wave], return_exceptions=True)
            for step, response in zip(wave, raw):
                if isinstance(response, Exception):
                    mark(step, StepStatus.FAILED, error_code="step_exception")
                    continue
                responses.append(response)
                outputs[step.step_id] = {"summary": response.content, "success": response.success, "evidence": response.tool_traces}
                findings.append({"step_id": step.step_id, "agent_type": response.agent_type.value, "content": response.content, "success": response.success, "evidence": response.tool_traces})
                finished = mark(step, StepStatus.SUCCESS if response.success else StepStatus.FAILED,
                                tools_used=response.tools_used, latency_ms=round(response.latency_ms, 1))
                await req.publish("step_finished", finished)

        if not responses:
            return failed("所有计划步骤均未成功，请转人工处理。", "plan_failed")
        primary = next((r for r in responses if r.agent_type == AgentType.TICKET), responses[0])
        content = findings[0]["content"] if len(findings) == 1 else "\n\n".join(f"【{f['agent_type']}】\n{f['content']}" for f in findings)
        status = "success" if not unsuccessful else "partial" if any(r.success for r in responses) else "failed"
        return OrchestratorResult(
            request_id=req.request_id,
            response=content,
            agent_type=primary.agent_type, intent=req.intent, escalated=any(r.escalate for r in responses),
            latency_ms=(time.monotonic() - started) * 1000,
            agent_types=list(dict.fromkeys(r.agent_type for r in responses)), primary_agent=primary.agent_type,
            supporting_agents=[r.agent_type for r in responses if r.agent_type != primary.agent_type],
            tools_used=list(dict.fromkeys(t for r in responses for t in r.tools_used)),
            tool_traces=[t for r in responses for t in r.tool_traces],
            routing_reason="bounded planner; parallel independent steps", routing_confidence=req.intent_confidence,
            status=status, retryable=status == "partial", plan=plan.to_dict(statuses), findings=findings, trace_events=step_rows,
            token_usage=_sum_usage(responses),
        )

    async def run_parallel(self, req: Request, decision: RoutingDecision) -> OrchestratorResult:
        started = time.monotonic()
        responses = await asyncio.gather(*[self._execute(req, t) for t in decision.agent_types], return_exceptions=True)
        valid = [r for r in responses if isinstance(r, AgentResponse)]
        if valid:
            content, error_code = "\n\n".join(r.content for r in valid), None
            status = "success" if len(valid) == len(responses) and all(r.success for r in valid) else "partial" if any(r.success for r in valid) else "failed"
        else:
            content, status, error_code = "所有 Agent 均处理失败。", "failed", "agent_failed"
        result = OrchestratorResult(
            request_id=req.request_id, response=content, agent_type=decision.primary_agent,
            intent=req.intent, escalated=any(r.escalate for r in valid), latency_ms=(time.monotonic() - started) * 1000,
            agent_types=[r.agent_type for r in valid], primary_agent=decision.primary_agent, supporting_agents=decision.supporting_agents,
            tools_used=[t for r in valid for t in r.tools_used], tool_traces=[t for r in valid for t in r.tool_traces],
            routing_reason=decision.reason, routing_confidence=decision.confidence, status=status, error_code=error_code,
            token_usage=_sum_usage(valid),
        )
        self._recent_tool_traces.append(result.tool_traces)
        return result

    def get_stats(self) -> Dict[str, Dict[str, Any]]:
        out = {}
        for agent_type, agents in self._pool.items():
            for i, a in enumerate(agents):
                recent_total, recent_success, recent_ms = a.stats.recent()
                out[f"{agent_type.value}_{i}"] = {
                    "total": a.stats.total, "success_rate": round(a.stats.success_rate, 3), "avg_ms": round(a.stats.avg_ms, 1),
                    "recent_total": recent_total, "recent_success_rate": round(recent_success, 3), "recent_avg_ms": round(recent_ms, 1),
                    "routing_score": round(a.stats.routing_score(), 3), "monitor_penalty": a.stats.monitor_penalty, "state": a.stats.state(),
                    "model": a._model, "tool_scope": list(a.get_tools()),
                }
        return out

    def update_routing_penalties(self, penalties: Dict[str, float]) -> None:
        for agent_type, agents in self._pool.items():
            for i, a in enumerate(agents):
                a.stats.set_penalty(penalties.get(f"{agent_type.value}_{i}", 0.))
