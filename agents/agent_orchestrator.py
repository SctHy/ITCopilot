"""编排：意图识别 → 单个 IT 服务 Agent（持有全部工具）执行 → 汇总结果。"""
from __future__ import annotations

import time
from typing import Any, Dict

from agents.base_agent import BaseAgent
from agents.checkpointing import execute_step
from agents.intent.recognizer import IntentRecognizer
from agents.intent.taxonomy import IntentCategory
from agents.tools import agent_tools, build_shared_rag_tools
from agents.types import OrchestratorResult, Request
from core.env import env_float
from core.llm import build_model
from tooling.executor import ToolExecutor


class AgentOrchestrator:
    def __init__(self, api_key="", base_url=None, model="deepseek-chat", skill_manager=None, recognizer=None, rag_tool_manager=None, **kwargs):
        self._llm = build_model(api_key, base_url, model)
        self._model = model
        self._intent_recognizer = recognizer or IntentRecognizer(api_key, base_url, model)
        self.tool_executor = ToolExecutor(timeout_s=env_float("IT_COPILOT_TOOL_TIMEOUT_S", 10))
        # 工具分三层，每次变更都从这三层重新组装，保证下线的外部工具会真正从 Agent 移除。
        self._shared_tools = build_shared_rag_tools(rag_tool_manager) if rag_tool_manager else {}
        self._external_tools: Dict[str, Any] = {}
        self.directory = None   # 台账读模型（WorkflowStore），由 API 装配时注入
        self._local_tools = agent_tools(lambda: self.directory)
        self.agent = BaseAgent(self._llm, model, skill_manager)
        self.agent.executor = self.tool_executor
        self._rebuild_tools()

    async def aclose(self) -> None:
        """释放外部客户端（意图识别的 Jev 连接池）。"""
        await self._intent_recognizer.aclose()

    def _rebuild_tools(self) -> None:
        # 本地工具优先于同名的共享/外部工具。
        self.agent.set_shared_tools({**self._shared_tools, **self._external_tools, **self._local_tools})

    def set_shared_tools(self, tools):
        self._shared_tools = dict(tools or {})
        self._rebuild_tools()

    def set_external_tools(self, tools):
        """外部 MCP 工具集合整体替换（连接/断开/工具列表变化时由 MCP 客户端回调）。"""
        self._external_tools = dict(tools or {})
        self._rebuild_tools()

    async def recognize_intent(self, message, history=None, previous_intent_group=None):
        return await self._intent_recognizer.recognize(message, history=history, previous_intent_group=previous_intent_group)

    async def _execute(self, req: Request):
        return await self.agent.handle(req)

    async def run(self, req: Request) -> OrchestratorResult:
        started = time.monotonic()
        if req.intent is None:
            ir = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent, req.intent_group, req.urgency, req.intent_confidence, req.entities = ir.intent, ir.intent_group, ir.urgency, ir.confidence, ir.entities
        req.stream_answer = True
        response = await execute_step(self._execute, req, "agent")
        return OrchestratorResult(
            request_id=req.request_id, response=response.content, intent=req.intent,
            escalated=response.escalate or req.intent == IntentCategory.HUMAN_HANDOFF, latency_ms=(time.monotonic() - started) * 1000,
            tools_used=response.tools_used, tool_traces=response.tool_traces, status=response.status,
            error_code=response.error_code, token_usage=response.token_usage,
        )

    def get_stats(self) -> Dict[str, Any]:
        stats = self.agent.stats
        recent_total, recent_success, recent_ms = stats.recent()
        return {
            "total": stats.total, "success_rate": round(stats.success_rate, 3), "avg_ms": round(stats.avg_ms, 1),
            "recent_total": recent_total, "recent_success_rate": round(recent_success, 3), "recent_avg_ms": round(recent_ms, 1),
            "model": self._model, "tool_scope": list(self.agent.get_tools()),
        }
