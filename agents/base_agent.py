"""BaseAgent：PydanticAI 工具循环、工具治理与追踪、提示组装与流式输出。"""
from __future__ import annotations

import asyncio
import hashlib
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
from agents.states import OnceState, once_state, MODEL_RUN, TOOL_CALL, SlotState, slot_state
from agents.intent.taxonomy import WRITE_ACTIONS
from agents.prompting import (
    AGENT_RULES,
    claim_problems,
    correction_prompt,
    fallback_note,
    history_messages,
    redact_user_secrets,
    strip_execution_notes,
    turn_prompt,
)
from agents.types import (
    AgentCallError,
    AgentCallResult,
    AgentProfile,
    AgentResponse,
    AgentRunDeps,
    ModelUnavailableError,
    Request,
)
from core.env import env_float
from core.protocols import CacheState, cache_state
from core.llm import agent_thinking_settings, is_model_unavailable
from core.redaction import redact
from monitor import metrics
from tooling.executor import ToolExecutor, ToolInputInvalid, WriteGuardBlocked, error_class

logger = logging.getLogger(__name__)

# Default model requests per agent run (each tool round + the final answer); override with
# IT_COPILOT_MAX_MODEL_ROUNDS. 3 cut off "diagnose, then file a ticket" (needs 4) on deepseek-flash.
MAX_MODEL_ROUNDS = 5
ESCALATION_MARKERS = ("转人工", "人工客服", "人工坐席", "无法处理", "escalate")


class BaseAgent:
    system_prompt = "你是 IT-Copilot 的 IT 服务 Agent：设备、网络与软件、账号权限、工单。一条消息含多个问题时逐个处理。"
    name = "it_copilot"

    def __init__(self, llm, model, skill_manager=None, profile=None):
        self._llm = llm
        self.profile = profile or AgentProfile()
        self._model = model
        self._skill_manager = skill_manager
        self.stats = AgentStats()
        self._shared_tools = {}
        self._agent = None
        self.executor = ToolExecutor()
        self._fingerprints = {}

    def get_tools(self):
        return dict(self._shared_tools)

    def set_shared_tools(self, tools):
        self._shared_tools = dict(tools or {})
        self._agent = None

    def _runtime(self) -> Agent:
        """按当前工具白名单懒构建 PydanticAI Agent；白名单变化时重建。"""
        if cache_state(self._agent is not None) == CacheState.MISSING or (self._skill_manager is not None and self._fingerprints["skill_fp"] != self._skill_manager.fingerprint):
            tools = [self._as_tool(spec) for spec in self.get_tools().values()]
            digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
            self._fingerprints = {
                "prompt_fp": digest(self.system_prompt + AGENT_RULES),
                "tools_fp": digest(json.dumps([[name, spec.input_schema] for name, spec in sorted(self.get_tools().items())], sort_keys=True, ensure_ascii=False)),
                "skill_fp": self._skill_manager.fingerprint if self._skill_manager else digest(""),
            }
            self._agent = Agent(deps_type=AgentRunDeps, output_type=str, instructions=self._instructions, tools=tools, name=self.name)
        return self._agent

    def fingerprints(self) -> dict:
        self._runtime()
        return dict(self._fingerprints)

    def _observability(self, req):
        from core.turn_records import build_sha
        sha = build_sha()
        return {**self.fingerprints(), "agent_spec": self.name, "agent_version": "build:" + sha,
                "build_sha": sha, "model": self._model, "route": "agent", "route_reason": "primary",
                "signals": list(req.signals), "first_token_ms": req.first_token_ms}

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
        t0 = req.started_at = time.monotonic()
        self.stats.total += 1
        try:
            result = await self._call_llm(req)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            ms = self._record(False, t0, req)
            logger.exception("agent LLM call failed")
            tools_used, tool_traces = getattr(ex, "tools_used", []), getattr(ex, "tool_traces", [])
            if isinstance(ex, ModelUnavailableError):
                logger.error("LLM service unavailable: url=%s error_type=%s detail=%s", ex.base_url, type(ex.__cause__ or ex).__name__, ex)
                response = AgentResponse("模型服务暂时不可用，请检查模型配置或稍后重试。", False, latency_ms=ms, tools_used=tools_used, tool_traces=tool_traces, status="failed", error_code="model_unavailable")
            else:
                response = AgentResponse("抱歉，模型暂时无法处理该请求，请稍后重试。", False, latency_ms=ms, tools_used=tools_used, tool_traces=tool_traces, status="failed", error_code="agent_failed")
            if isinstance(ex, AgentCallError) and ex.reason in {"round_limit", "bad_output", "empty_output"}:
                response.error_code = ex.reason
            response.observability = self._observability(req)
            return self._observe(response, getattr(ex, "reason", "exception"))
        ok = result.status in {"success", "partial"}
        self.stats.success += int(ok)
        ms = self._record(ok, t0, req)
        return self._observe(AgentResponse(
            content=result.content, success=ok, confidence=req.intent_confidence, latency_ms=ms,
            escalate=self._needs_escalation(result.content), tools_used=result.tools_used, tool_traces=result.tool_traces,
            status=result.status, token_usage=result.token_usage, observability=self._observability(req),
        ))

    def _observe(self, response: AgentResponse, failure_reason: Optional[str] = None) -> AgentResponse:
        metrics.AGENT_REQUESTS.labels(status=response.status, error_code=response.error_code or "none").inc()
        if failure_reason:
            metrics.AGENT_FAILURES.labels(reason=failure_reason).inc()
        metrics.AGENT_LATENCY.observe(response.latency_ms / 1000)
        for direction in ("input", "output", "cache_read"):
            tokens = response.token_usage.get(f"{direction}_tokens", 0)
            if tokens:
                metrics.LLM_TOKENS.labels(model=self._model or "unknown", direction=direction).inc(tokens)
        return response

    def _record(self, success: bool, started: float, req: Request) -> float:
        ms = (time.monotonic() - started) * 1000
        self.stats.total_ms += ms
        self.stats.record(success, ms)
        return ms

    async def _call_llm(self, req: Request) -> AgentCallResult:
        flow = MODEL_RUN.start()
        flow.send("run")
        deps = AgentRunDeps(req)
        if self._skill_manager and self._skill_manager.disclosure() == "progressive":
            req.skill_text = self._skill_manager.selected_prompt(req.message, req.intent_group, req.allowed_writes or ())
        settings = ModelSettings(**agent_thinking_settings(self.profile), request_timeout=max(1, env_float("IT_COPILOT_LLM_TIMEOUT_S", 90)))
        rounds = max(1, int(env_float("IT_COPILOT_MAX_MODEL_ROUNDS", MAX_MODEL_ROUNDS)))
        kwargs = dict(deps=deps, model=self._llm, message_history=history_messages(req.history), model_settings=settings, usage_limits=UsageLimits(request_limit=rounds))

        async def run(prompt, **overrides):
            if req.stream_answer and req.emit is not None:
                return await self._run_streaming(prompt, req, {**kwargs, **overrides})
            return await self._runtime().run(prompt, **{**kwargs, **overrides})

        try:
            result = await run(turn_prompt(req))
            usages = [result.usage]
            problems = claim_problems(result.output or "", deps, req)
            if problems:
                flow.send("correct")
                # 回答与本轮实际业务结果不一致（无据的“已提交”、失败未说明、无据的“已撤回”）：
                # 带上具体不一致之处让模型补调工具或改口，只纠正一次。
                logger.warning("agent reply disagrees with this turn's write results (%d); asking once to correct", len(problems))
                metrics.AGENT_CLAIM_CHECKS.labels(outcome="corrected").inc()
                req.signals.append("claim_corrected")
                await req.publish("answer_reset", {})
                result = await run(correction_prompt(problems), message_history=result.all_messages())
                usages.append(result.usage)
        except asyncio.CancelledError:
            flow.send("cancel")
            raise
        except UsageLimitExceeded:
            flow.send("fail")
            raise AgentCallError("工具调用超过最大轮数", deps.used, deps.traces, reason="round_limit") from None
        except UnexpectedModelBehavior as exc:
            flow.send("fail")
            raise AgentCallError(f"模型返回无法处理的结果（{type(exc).__name__}）", deps.used, deps.traces, reason="bad_output") from exc
        except Exception as exc:
            flow.send("fail")
            if is_model_unavailable(exc):
                base_url = os.getenv("OPENAI_BASE_URL", "")
                raise ModelUnavailableError(f"模型服务请求失败（{type(exc).__name__}）", deps.used, deps.traces, base_url) from exc
            raise
        content = strip_execution_notes(result.output or "")
        if not content:
            flow.send("fail")
            raise AgentCallError("模型未返回有效回复", deps.used, deps.traces, reason="empty_output")
        remaining = claim_problems(content, deps, req)
        if remaining:
            # 纠正一次后仍不一致：不再请求模型，追加以审批记录为准的确定性说明。
            logger.warning("agent reply still disagrees with write results after correction; appending note")
            metrics.AGENT_CLAIM_CHECKS.labels(outcome="noted").inc()
            req.signals.append("claim_noted")
            content += "\n\n" + fallback_note(remaining)
        content = redact_user_secrets(content, req.message)
        status = "partial" if result.response.finish_reason == "length" else "success"
        flow.send("truncate" if status == "partial" else "complete")
        return AgentCallResult(content, deps.used, deps.traces, status, {key: sum(int(getattr(u, key) or 0) for u in usages)
                                                                          for key in ("requests", "input_tokens", "output_tokens", "cache_read_tokens")})


    async def _run_streaming(self, prompt: str, req: Request, kwargs: Dict[str, Any]):
        """边生成边推送 answer_delta。带工具调用的那轮回复里的文字只是过渡语，推送 answer_reset 让前端清掉草稿。"""
        buffer: List[str] = []

        async def flush():
            if buffer:
                await req.publish("answer_delta", {"delta": "".join(buffer)})
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
                    await req.publish("answer_reset", {})
        return run.result

    async def _invoke_tool(self, deps: AgentRunDeps, spec, args: Dict[str, Any], tool_id: Optional[str]):
        """单次工具调用：Schema 校验、写操作守卫，经 ToolExecutor（超时/熔断/只读重试）执行并记录 trace；失败以错误结果回传模型。"""
        req = deps.req
        flow = TOOL_CALL.start()
        flow.send("validate")
        started = time.monotonic()
        success, error, outcome = True, "", "success"
        idempotency_key = None if spec.retry_safe else self.executor.idempotency_key(req.request_id, spec.name, args)
        await req.publish("tool_started", {"tool": spec.name, "tool_use_id": tool_id, "retry_safe": spec.retry_safe})
        try:
            if spec.name in req.hidden_tools:
                raise WriteGuardBlocked(f"本次处理不提供 {spec.name}")
            self._validate_tool_input(spec, args)
            if not spec.retry_safe:
                await self._guard_unsafe_tool(req, spec.name, args)
            flow.send("execute")
            result = await self.executor.run(spec, req, args)
            deps.used.append(spec.name)
            flow.send("complete")
        except asyncio.CancelledError:
            flow.send("cancel")
            raise
        except Exception as ex:
            flow.send("fail")
            success, error = False, str(ex)
            outcome = error_class(ex)
            result = {"success": False, "error": error}
            logger.warning("tool %s failed: %s", spec.name, ex)
        latency_ms = round((time.monotonic() - started) * 1000, 1)
        metrics.AGENT_TOOL_CALLS.labels(tool=spec.name, error_class=outcome).inc()
        deps.traces.append({
            "tool_name": spec.name, "tool_use_id": tool_id, "input": args, "success": success,
            "latency_ms": latency_ms, "error": error, "retry_safe": spec.retry_safe, "outcome": outcome, "error_class": outcome,
            "idempotency_key": idempotency_key, "output": redact(result),
            "phase": flow.state,
        })
        await req.publish("tool_finished", {"tool": spec.name, "tool_use_id": tool_id,
                                            "success": success, "outcome": outcome, "latency_ms": latency_ms,
                                            "requires_approval": isinstance(result, dict) and bool(result.get("requires_approval"))})
        return result

    @staticmethod
    async def _guard_unsafe_tool(req: Request, name: str, args: Dict[str, Any]) -> None:
        """写操作前置条件：槽位齐全、同请求不重复、先提交“结果未知”回执再执行。"""
        if req.allowed_writes is not None and name in WRITE_ACTIONS and name not in req.allowed_writes:
            # 意图识别判断用户这一轮没有要求这项操作：挡住主动提交，而不是从工具列表里拿掉（保持前缀缓存）。
            raise WriteGuardBlocked("用户这一轮没有要求办理这项操作；需要的话先向用户确认")
        missing_slots = (req.conversation_state or {}).get("missing_fields") or []
        if slot_state(req.conversation_state) == SlotState.CLARIFYING:
            raise WriteGuardBlocked("缺少必要信息，暂不执行写操作：" + ", ".join(missing_slots))
        key = json.dumps([name, args], sort_keys=True, ensure_ascii=True)
        if once_state(key, req.unsafe_tool_calls) == OnceState.CONSUMED:
            raise WriteGuardBlocked("副作用工具不得在同一请求重复执行")
        req.unsafe_tool_calls.add(key)
        req.tool_attempts.append({"tool_name": name, "retry_safe": False})
        if req.before_unsafe_tool:
            await req.before_unsafe_tool(name)

    def _build_system_prompt(self, req: Request) -> str:
        # 只放固定不变的内容：system 与工具定义构成稳定前缀，模型服务的前缀缓存才能跨请求命中。
        prompt = f"{self.system_prompt}\n{AGENT_RULES}"
        if not self._skill_manager:
            return prompt
        # skill 放在 system 里：可信、且对每个请求固定。渐进式披露时 system 只放目录（同样固定），
        # 正文按本轮相关性放进本轮消息（见 _call_llm 与 turn_prompt）。
        if self._skill_manager.disclosure() == "progressive":
            index = self._skill_manager.index()
            return prompt + ("\n\n业务规范目录（本轮相关的条目随本轮消息附上；与上面的规则冲突时以规则为准）：\n" + index if index else "")
        skills = self._skill_manager.prompt()
        if skills:
            prompt += "\n\n业务规范（与上面的规则冲突时以规则为准）：\n" + skills
        return prompt

    @staticmethod
    def _validate_tool_input(spec, args) -> None:
        from jsonschema import ValidationError, validators
        try:
            validators.validator_for(spec.input_schema)(spec.input_schema).validate(args)
        except ValidationError as ex:
            raise ToolInputInvalid(f"工具参数未通过 JSON Schema 校验（{ex.validator}）") from None

    @staticmethod
    def _needs_escalation(content: str) -> bool:
        return any(marker in content for marker in ESCALATION_MARKERS)

