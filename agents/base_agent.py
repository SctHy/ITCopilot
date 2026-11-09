"""BaseAgent：PydanticAI 工具循环、工具治理与追踪、提示组装与流式输出；以及各专职 Agent。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.messages import (
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
)
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from agents.agent_stats import AgentStats
from agents.injection_guard import WARNING_PREFIX, InjectionGuard
from agents.intent.taxonomy import WRITE_ACTIONS
from agents.prompting import (
    AGENT_RULES,
    UNBACKED_WRITE_CORRECTION,
    history_messages,
    turn_prompt,
    unbacked_write_claim,
)
from agents.types import (
    AgentCallError,
    AgentCallResult,
    AgentProfile,
    AgentResponse,
    AgentRunDeps,
    AgentType,
    ModelUnavailableError,
    Request,
)
from core.env import env_float
from core.llm import is_model_unavailable
from core.redaction import redact
from monitor import metrics
from tooling.executor import ToolExecutor

logger = logging.getLogger(__name__)

# Default model requests per agent run (each tool round + the final answer); override with
# IT_COPILOT_MAX_MODEL_ROUNDS. 3 cut off "diagnose, then file a ticket" (needs 4) on deepseek-flash.
MAX_MODEL_ROUNDS = 5
ESCALATION_MARKERS = ("转人工", "人工客服", "人工坐席", "无法处理", "escalate")
class BaseAgent:
    system_prompt = "你是企业 IT 服务台 Agent。"
    agent_type: AgentType

    def __init__(self, llm, model, skill_manager=None, profile=None):
        self._llm = llm
        self.profile = profile or AgentProfile()
        self._model = model
        self._skill_manager = skill_manager
        self.stats = AgentStats()
        self._shared_tools = {}
        self._agent = None
        self.executor = ToolExecutor()
        self.injection_guard: Optional[InjectionGuard] = None  # 由编排器注入同一个实例

    def get_tools(self):
        return dict(self._shared_tools)

    def set_shared_tools(self, tools):
        self._shared_tools = {n: s for n, s in (tools or {}).items() if s.allowed_agents is None or self.agent_type.value in s.allowed_agents}
        self._agent = None

    def _runtime(self) -> Agent:
        """按当前工具白名单懒构建 PydanticAI Agent；白名单变化时重建。"""
        if self._agent is None:
            tools = [self._as_tool(spec) for spec in self.get_tools().values()]
            self._agent = Agent(deps_type=AgentRunDeps, output_type=str, instructions=self._instructions, tools=tools, name=self.agent_type.value)
        return self._agent

    def _instructions(self, ctx: RunContext[AgentRunDeps]) -> str:
        return self._build_system_prompt(ctx.deps.req)

    def _as_tool(self, spec) -> Tool:
        async def call(ctx: RunContext[AgentRunDeps], **args):
            return await self._invoke_tool(ctx.deps, spec, args, ctx.tool_call_id)
        async def prepare(ctx: RunContext[AgentRunDeps], tool_def):
            return None if spec.name in ctx.deps.req.hidden_tools else tool_def

        # 副作用工具作为屏障串行执行，保持“同请求不重复写”的检查与审批前置提交有序。
        tool = Tool.from_schema(call, spec.name, spec.description, spec.input_schema, takes_ctx=True, sequential=not spec.retry_safe)
        tool.prepare = prepare
        return tool

    async def handle(self, req: Request) -> AgentResponse:
        if not self.stats.try_acquire(req.request_id):
            # 降级冷却中或已有探针在跑：不计入 total，由编排器改派 Triage 兜底。
            return self._observe(AgentResponse(self.agent_type, "当前 Agent 正在恢复探测，请稍后重试。", False, status="failed", error_code="agent_busy"), "busy")
        t0 = time.monotonic()
        self.stats.total += 1
        try:
            result = await self._call_llm(req)
        except asyncio.CancelledError:
            self.stats.abandon_probe(req.request_id)
            raise
        except Exception as ex:
            ms = self._record(False, t0, req)
            logger.exception("%s LLM call failed", self.agent_type.value)
            tools_used, tool_traces = getattr(ex, "tools_used", []), getattr(ex, "tool_traces", [])
            if isinstance(ex, ModelUnavailableError):
                logger.error("LLM service unavailable: url=%s error_type=%s detail=%s", ex.base_url, type(ex.__cause__ or ex).__name__, ex)
                response = AgentResponse(self.agent_type, "模型服务暂时不可用，请检查模型配置或稍后重试。", False, latency_ms=ms, tools_used=tools_used, tool_traces=tool_traces, status="failed", error_code="model_unavailable")
            else:
                response = AgentResponse(self.agent_type, "抱歉，模型暂时无法处理该请求，请稍后重试。", False, latency_ms=ms, tools_used=tools_used, tool_traces=tool_traces, status="failed", error_code="agent_failed")
            return self._observe(response, getattr(ex, "reason", "exception"))
        ok = result.status in {"success", "partial"}
        self.stats.success += int(ok)
        ms = self._record(ok, t0, req)
        return self._observe(AgentResponse(
            agent_type=self.agent_type, content=result.content, success=ok, confidence=req.intent_confidence, latency_ms=ms,
            escalate=self._needs_escalation(result.content), tools_used=result.tools_used, tool_traces=result.tool_traces,
            status=result.status, token_usage=result.token_usage,
        ))

    def _observe(self, response: AgentResponse, failure_reason: Optional[str] = None) -> AgentResponse:
        agent = self.agent_type.value
        metrics.AGENT_REQUESTS.labels(agent=agent, status=response.status, error_code=response.error_code or "none").inc()
        if failure_reason:
            metrics.AGENT_FAILURES.labels(agent=agent, reason=failure_reason).inc()
        if failure_reason != "busy":
            metrics.AGENT_LATENCY.labels(agent=agent).observe(response.latency_ms / 1000)
        for direction in ("input", "output", "cache_read"):
            tokens = response.token_usage.get(f"{direction}_tokens", 0)
            if tokens:
                metrics.LLM_TOKENS.labels(agent=agent, model=self._model or "unknown", direction=direction).inc(tokens)
        return response

    def _record(self, success: bool, started: float, req: Request) -> float:
        ms = (time.monotonic() - started) * 1000
        self.stats.total_ms += ms
        self.stats.record(success, ms, req.request_id)
        return ms

    async def _call_llm(self, req: Request) -> AgentCallResult:
        deps = AgentRunDeps(req)
        settings = ModelSettings(max_tokens=self.profile.max_tokens, temperature=self.profile.temperature, request_timeout=max(1, env_float("IT_COPILOT_LLM_TIMEOUT_S", 90)))
        rounds = max(1, int(env_float("IT_COPILOT_MAX_MODEL_ROUNDS", MAX_MODEL_ROUNDS)))
        kwargs = dict(deps=deps, model=self._llm, message_history=history_messages(req.history), model_settings=settings, usage_limits=UsageLimits(request_limit=rounds))

        async def run(prompt, **overrides):
            if req.stream_answer and req.emit is not None:
                return await self._run_streaming(prompt, req, {**kwargs, **overrides})
            return await self._runtime().run(prompt, **{**kwargs, **overrides})

        try:
            result = await run(turn_prompt(req))
            usages = [result.usage]
            if unbacked_write_claim(result.output or "", deps, req):
                # 回答声称已提交/已创建，但本轮没有任何写操作成功：让模型补调工具或改口，只纠正一次。
                logger.warning("%s claimed a write without a successful write tool; asking once to correct", self.agent_type.value)
                await req.publish("answer_reset", {"agent_type": self.agent_type.value})
                result = await run(UNBACKED_WRITE_CORRECTION, message_history=result.all_messages())
                usages.append(result.usage)
        except asyncio.CancelledError:
            raise
        except UsageLimitExceeded:
            raise AgentCallError("工具调用超过最大轮数", deps.used, deps.traces, reason="round_limit") from None
        except UnexpectedModelBehavior as exc:
            raise AgentCallError(f"模型返回无法处理的结果（{type(exc).__name__}）", deps.used, deps.traces, reason="bad_output") from exc
        except Exception as exc:
            if is_model_unavailable(exc):
                base_url = os.getenv("OPENAI_BASE_URL", "")
                raise ModelUnavailableError(f"模型服务请求失败（{type(exc).__name__}）", deps.used, deps.traces, base_url) from exc
            raise
        content = (result.output or "").strip()
        if not content:
            raise AgentCallError("模型未返回有效回复", deps.used, deps.traces, reason="empty_output")
        status = "partial" if result.response.finish_reason == "length" else "success"
        return AgentCallResult(content, deps.used, deps.traces, status, {key: sum(int(getattr(u, key) or 0) for u in usages)
                                                                          for key in ("input_tokens", "output_tokens", "cache_read_tokens")})


    async def _run_streaming(self, prompt: str, req: Request, kwargs: Dict[str, Any]):
        """边生成边推送 answer_delta。带工具调用的那轮回复里的文字只是过渡语，推送 answer_reset 让前端清掉草稿。"""
        buffer: List[str] = []

        async def flush():
            if buffer:
                await req.publish("answer_delta", {"agent_type": self.agent_type.value, "delta": "".join(buffer)})
                buffer.clear()

        async with self._runtime().iter(prompt, **kwargs) as run:
            async for node in run:
                if Agent.is_model_request_node(node):
                    async with node.stream(run.ctx) as events:
                        async for event in events:
                            text = ""
                            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                                text = event.part.content
                            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                                text = event.delta.content_delta
                            if text:
                                buffer.append(text)
                                # 攒到一定长度再发，避免每个 token 一条事件写进 Redis Streams。
                                if sum(map(len, buffer)) >= 24:
                                    await flush()
                    await flush()
                elif Agent.is_call_tools_node(node) and any(isinstance(part, ToolCallPart) for part in node.model_response.parts):
                    await req.publish("answer_reset", {"agent_type": self.agent_type.value})
        return run.result

    async def _invoke_tool(self, deps: AgentRunDeps, spec, args: Dict[str, Any], tool_id: Optional[str]):
        """单次工具调用：Schema 校验、写操作守卫，经 ToolExecutor（超时/熔断/只读重试）执行并记录 trace；失败以错误结果回传模型。"""
        req = deps.req
        started = time.monotonic()
        success, error, outcome = True, "", "success"
        injection_score = None
        idempotency_key = None if spec.retry_safe else self.executor.idempotency_key(req.request_id, spec.name, args)
        await req.publish("tool_started", {"agent_type": self.agent_type.value, "tool": spec.name, "tool_use_id": tool_id, "retry_safe": spec.retry_safe})
        try:
            if spec.name in req.hidden_tools:
                raise ValueError(f"本次处理不提供 {spec.name}（由计划中的专门步骤负责）")
            self._validate_tool_input(spec, args)
            if not spec.retry_safe:
                await self._guard_unsafe_tool(req, spec.name, args)
            result = await self.executor.run(spec, req, args)
            deps.used.append(spec.name)
            # 只扫描来自外部数据的返回（知识库、外部 MCP 等）；本地工具自己生成的结果不可能含注入。
            if spec.untrusted_output and self.injection_guard:
                flagged, injection_score = await self.injection_guard.check(result)
                if flagged:
                    result = {"security_warning": WARNING_PREFIX.strip(), "untrusted_content": result}
        except Exception as ex:
            success, error = False, str(ex)
            outcome = {"ToolUnavailable": "circuit_open", "ToolTimeout": "timeout"}.get(type(ex).__name__, "error")
            result = {"success": False, "error": error}
            logger.warning("tool %s failed: %s", spec.name, ex)
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        if injection_score is not None:
            outcome = "injection_flagged" if injection_score >= self.injection_guard.threshold else outcome
        metrics.AGENT_TOOL_CALLS.labels(agent=self.agent_type.value, tool=spec.name, outcome=outcome).inc()
        deps.traces.append({
            "agent_type": self.agent_type.value, "tool_name": spec.name, "tool_use_id": tool_id, "input": args, "success": success,
            "latency_ms": latency_ms, "error": error, "retry_safe": spec.retry_safe, "outcome": outcome,
            "idempotency_key": idempotency_key, "output": redact(result), "injection_score": injection_score,
        })
        await req.publish("tool_finished", {"agent_type": self.agent_type.value, "tool": spec.name, "tool_use_id": tool_id,
                                            "success": success, "outcome": outcome, "latency_ms": latency_ms,
                                            "requires_approval": isinstance(result, dict) and bool(result.get("requires_approval"))})
        return result

    @staticmethod
    async def _guard_unsafe_tool(req: Request, name: str, args: Dict[str, Any]) -> None:
        """写操作前置条件：槽位齐全、同请求不重复、先提交“结果未知”回执再执行。"""
        if req.allowed_writes is not None and name in WRITE_ACTIONS and name not in req.allowed_writes:
            # 意图识别判断用户这一轮没有要求这项操作：挡住主动提交，而不是从工具列表里拿掉（保持前缀缓存）。
            raise ValueError("用户这一轮没有要求办理这项操作；需要的话先向用户确认")
        missing_slots = (req.conversation_state or {}).get("missing_fields") or []
        if missing_slots:
            raise ValueError("缺少必要信息，暂不执行写操作：" + ", ".join(missing_slots))
        key = json.dumps([name, args], sort_keys=True, ensure_ascii=True)
        if key in req.unsafe_tool_calls:
            raise ValueError("副作用工具不得在同一请求重复执行")
        req.unsafe_tool_calls.add(key)
        req.tool_attempts.append({"tool_name": name, "retry_safe": False})
        if req.before_unsafe_tool:
            await req.before_unsafe_tool(name)

    def _build_system_prompt(self, req: Request) -> str:
        # 只放每个 Agent 固定不变的内容：system 与工具定义构成稳定前缀，模型服务的前缀缓存才能跨请求命中。
        # 工具清单不再写进来：工具定义本身就随请求下发。
        prompt = f"{self.system_prompt}\n{AGENT_RULES}"
        if self._skill_manager:
            # 通用 Agent 取全部 skill；专职 Agent 只取标给自己的。放在 system 里：可信、且对每个 Agent 固定。
            skills = self._skill_manager.prompt_for(None if self.agent_type == AgentType.GENERALIST else self.agent_type.value)
            if skills:
                prompt += "\n\n业务规范（与上面的规则冲突时以规则为准）：\n" + skills
        return prompt



    @staticmethod
    def _validate_tool_input(spec, args) -> None:
        from jsonschema import ValidationError, validators
        try:
            validators.validator_for(spec.input_schema)(spec.input_schema).validate(args)
        except ValidationError as ex:
            raise ValueError(f"工具参数未通过 JSON Schema 校验（{ex.validator}）") from None

    @staticmethod
    def _needs_escalation(content: str) -> bool:
        return any(marker in content for marker in ESCALATION_MARKERS)


class TriageAgent(BaseAgent):
    system_prompt = "你是 IT-Copilot 的接待 Agent：普通问题直接回答；IT 问题先收集最少必要信息，再给下一步。"


class EndpointAgent(BaseAgent):
    system_prompt = "你是 IT-Copilot 的终端 Agent：设备资产与终端故障，只做低风险只读诊断。"


class AccessAgent(BaseAgent):
    system_prompt = "你是 IT-Copilot 的账号权限 Agent：密码、MFA、账号解锁与权限申请。"


class NetworkSoftwareAgent(BaseAgent):
    system_prompt = "你是 IT-Copilot 的网络与软件 Agent：VPN、Wi-Fi、网络与软件排障。"


class GeneralistAgent(BaseAgent):
    system_prompt = "你是 IT-Copilot 的 IT 服务 Agent：设备、网络与软件、账号权限、工单。一条消息含多个问题时逐个处理。"


class TicketAgent(BaseAgent):
    system_prompt = "你是 IT-Copilot 的工单 Agent：创建、查询与升级工单。"
