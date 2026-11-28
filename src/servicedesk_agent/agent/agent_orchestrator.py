"""编排：意图识别 → 单个 IT 服务 Agent（持有全部工具）执行 → 汇总结果。"""
from __future__ import annotations

import time

from servicedesk_agent.agent.base_agent import BaseAgent
from servicedesk_agent.agent.intent.recognizer import IntentRecognizer
from servicedesk_agent.agent.intent.taxonomy import IntentCategory
from servicedesk_agent.agent.tools import agent_tools, build_shared_rag_tools
from servicedesk_agent.agent.types import OrchestratorResult, Request
from servicedesk_agent.infra.env import env_float
from servicedesk_agent.agent.model_router import ModelRouter
from servicedesk_agent.agent.tooling.executor import ToolExecutor


class AgentOrchestrator:
    def __init__(self, api_key="", base_url=None, model="", skill_manager=None, recognizer=None, rag_tool_manager=None, router=None,
                 *, directory=None, knowledge_version=None):
        self.router = ModelRouter(api_key=api_key, base_url=base_url, model=model) if router is None else router
        self._intent_recognizer = recognizer or IntentRecognizer(api_key, base_url, model, router=self.router)
        self.tool_executor = ToolExecutor(timeout_s=env_float("SERVICEDESK_AGENT_TOOL_TIMEOUT_S", 10))
        self._shared_tools = build_shared_rag_tools(rag_tool_manager) if rag_tool_manager else {}
        self.knowledge_version = knowledge_version or (lambda tenant: 0.0)
        self.directory = directory   # 员工与权限记录查询服务（WorkflowStore），由 API 启动时注入
        self._local_tools = agent_tools(lambda: self.directory, include_devsuite=directory is not None)
        self.agent = BaseAgent(None, model, skill_manager, router=self.router)
        self.agent.executor = self.tool_executor
        self.agent.set_shared_tools({**self._shared_tools, **self._local_tools})

    def set_shared_tools(self, tools):
        self._shared_tools = dict(tools or {})
        self.agent.set_shared_tools({**self._shared_tools, **self._local_tools})

    async def recognize_intent(self, message, history=None, previous_intent_group=None):
        return await self._intent_recognizer.recognize(message, history=history, previous_intent_group=previous_intent_group)

    async def run(self, req: Request) -> OrchestratorResult:
        started = time.monotonic()
        if req.intent is None:
            ir = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent, req.intent_group, req.urgency, req.intent_confidence, req.entities = ir.intent, ir.intent_group, ir.urgency, ir.confidence, ir.entities
            req.allowed_writes = ir.requested_actions
        req.stream_answer = True
        response = await self.agent.handle(req)
        return OrchestratorResult(
            request_id=req.request_id, response=response.content, intent=req.intent,
            escalated=response.escalate or req.intent == IntentCategory.HUMAN_HANDOFF, latency_ms=(time.monotonic() - started) * 1000,
            tools_used=response.tools_used, tool_traces=response.tool_traces, status=response.status,
            error_code=response.error_code, token_usage=response.token_usage, observability=response.observability, new_messages=response.new_messages,
        )
