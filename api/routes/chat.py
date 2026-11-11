"""对话：同步 /chat、实时事件流 /chat/stream，以及 IM 渠道复用的同一条对话路径。"""
import asyncio
import json
import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse

from agents.states import TURN
from api import state
from api.deps import current_principal, forbid, request_id, spawn
from api.schemas import ChatRequest, ChatResponse
from core.env import env_float
from core.events import TERMINAL_EVENTS, EventPublisher, StreamConflict, request_fingerprint
from core.identity import Principal
from monitor import metrics
from workflow.policy import PolicyViolation
from workflow.store import ApprovalConflict

logger = logging.getLogger("it_copilot")

router = APIRouter()


class _Stages:
    """一轮对话各阶段耗时（毫秒），同时写入 it_copilot_chat_stage_seconds。"""

    def __init__(self):
        self.ms: Dict[str, float] = {}

    @contextmanager
    def __call__(self, stage: str):
        started = time.monotonic()
        try:
            yield
        finally:
            elapsed = time.monotonic() - started
            self.ms[stage] = round(self.ms.get(stage, 0.0) + elapsed * 1000, 1)
            metrics.CHAT_STAGE_LATENCY.labels(stage=stage).observe(elapsed)


async def _prefetch_knowledge(message: str, department):
    """预取只是加速手段：检索出错时当作没有结果，不能拖垮整轮（Agent 仍可自己调用检索工具）。"""
    if not state.tool_manager:
        return None
    try:
        return await state.tool_manager.search("knowledge_search", message, top_k=3, department=department)
    except Exception as exc:
        logger.warning("knowledge prefetch failed: %s", type(exc).__name__)
        return None


async def _run_turn(payload: ChatRequest, principal: Principal, rid: str, conv_id: str, stages: _Stages, emit=None):
    """一次对话回合：读取记忆 → 意图识别 → 组装请求 → 编排执行。用户与部门取自已验证身份。"""
    from agents.conversation_state import known_entities, merge_state, with_login_employee
    from agents.tools import slim_hit
    from agents.types import Request

    user_id = principal.subject
    with stages("memory_context"):
        mem_ctx = await state.memory.get_context(principal.scope_key, conv_id, query=payload.message) if state.memory else None
    history = mem_ctx.history_turns() if mem_ctx and mem_ctx.recent_messages else None
    conv_state = (getattr(mem_ctx, "state", {}) or {}) if mem_ctx else {}
    with stages("intent"):
        # 知识库预取与意图识别并行：本地检索几十毫秒，用不用由意图识别（Jev）判断，省掉 Agent 专门检索的那一轮。
        intent_result, prefetched = await asyncio.gather(
            state.orchestrator.recognize_intent(payload.message, history=history[-5:] if history else None, previous_intent_group=conv_state.get("intent_group")),
            _prefetch_knowledge(payload.message, principal.department),
        )
    if emit:
        await emit("intent", {"intent": intent_result.intent.value, "intent_group": intent_result.intent_group,
                              "confidence": round(intent_result.confidence, 4), "source": intent_result.recognition_source})

    req = Request(
        message=payload.message, user_id=user_id, conv_id=conv_id, context=mem_ctx.to_prompt_text(include_recent=False) if mem_ctx else "",
        history=history, entities=intent_result.entities, intent=intent_result.intent, intent_group=intent_result.intent_group,
        urgency=intent_result.urgency, intent_confidence=intent_result.confidence, department=principal.department, request_id=rid,
        principal=principal, emit=emit, allowed_writes=intent_result.requested_actions,
        knowledge=[slim_hit(x) for x in prefetched.data] if intent_result.needs_knowledge and prefetched and prefetched.success else [],
    )
    req.context += f"\n[当前登录员工]\n工号 {principal.employee_id or '未绑定'}；部门 {principal.department}"
    if state.task_recall:
        try:
            req.task_references = await asyncio.wait_for(state.task_recall.recall(principal, payload.message), 3)
            req.context += state.task_recall.prompt(req.task_references)
        except Exception:
            logger.warning('task recall unavailable; continuing without task references')
    # 缺失字段按跨轮累积的实体计算：上一轮已给出的工号等不应再次阻断写操作。
    entities = with_login_employee(intent_result.entities, conv_state, principal.employee_id, intent_result.intent.value)
    req.conversation_state = merge_state(conv_state, intent_result.intent_group, entities, payload.message, intent_result.missing_slots, intent=intent_result.intent.value)
    req.entities = known_entities(req.conversation_state, entities)

    async def before_unsafe_tool(name):
        # Chat tools only prepare immutable approval data. Actual effects happen in the
        # execution worker after approval, so a crashed dry-run is retryable.
        if not state.workflow:
            raise ValueError("审批存储不可用，无法创建写操作申请")

    req.before_unsafe_tool = before_unsafe_tool
    with stages("agent"):
        result = await state.orchestrator.run(req)
    return result, req, intent_result, mem_ctx


def _build_trace(result, intent_result, mem_ctx, approval, approval_id, principal: Principal, stage_ms: Dict[str, float]) -> Dict[str, Any]:
    trace = json.loads(json.dumps(result.__dict__, default=lambda value: value.value if hasattr(value, "value") else str(value)))
    trace["observability"] = {
        "schema_version": 3,
        "owner": principal.subject,
        "tenant": principal.tenant,
        "department": principal.department,
        "memory": {
            "working_hit": bool(mem_ctx and mem_ctx.recent_messages),
            "episodic_hits": len(mem_ctx.relevant_history) if mem_ctx else 0,
            "profile_hit": bool(mem_ctx and mem_ctx.user_profile),
        },
        "intent": {"source": intent_result.recognition_source, "confidence": intent_result.confidence},
        "approval": {"required": bool(approval), "approval_id": approval_id},
        "fallback": {"intent_source": intent_result.recognition_source},
        "stage_ms": stage_ms,
    }
    return trace


@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, principal: Principal = Depends(current_principal)):
    return await _chat_entry(payload, principal, request_id(principal, payload.client_request_id))


async def _chat_entry(payload: ChatRequest, principal: Principal, rid: str, emit=None):
    if not state.orchestrator:
        raise HTTPException(503, "service not ready")
    from monitor.telemetry import chat_span
    with chat_span(rid) as span:
        response = await _chat(payload, principal, rid, emit)
        if isinstance(response, ChatResponse):
            for key in ("status", "agent_type", "intent", "intent_group"):
                span.set_attribute(f"it_copilot.{key}", getattr(response, key))
        return response


async def _chat(payload: ChatRequest, principal: Principal, rid: str, emit=None):
    request_started = time.monotonic()
    conv_id = payload.conv_id
    if state.memory:
        async with state.memory.conversation(principal.scope_key, conv_id):
            receipt = await state.memory.get_turn_receipt(principal.scope_key, conv_id, rid)
            if receipt:
                if receipt.get("message") != payload.message:
                    raise HTTPException(409, "同一 client_request_id 不能用于不同消息")
                flow = TURN.start()
                flow.send("replay")
                return receipt.get("response")
            return await _complete_turn(payload, principal, rid, conv_id, request_started, emit)
    return await _complete_turn(payload, principal, rid, conv_id, request_started, emit)


async def _complete_turn(payload, principal: Principal, rid, conv_id, request_started, emit=None):
    # The caller holds the conversation lease through approval creation AND commit.
    flow = TURN.start()
    flow.send("run")
    try:
        return await _complete_turn_run(payload, principal, rid, conv_id, request_started, emit, flow)
    except asyncio.CancelledError:
        flow.send("cancel")
        raise
    except Exception:
        flow.send("fail")
        raise


async def _complete_turn_run(payload, principal, rid, conv_id, request_started, emit, flow):
    stages = _Stages()
    result, req, intent_result, mem_ctx = await _run_turn(payload, principal, rid, conv_id, stages, emit)

    from workflow.approval import approval_from_traces
    approval = approval_from_traces(result.tool_traces, payload.message)
    approval_id = rid + ":approval" if approval else None
    reply = result.response
    if approval and state.workflow:
        flow.send("propose")
        summary = {k: approval[k] for k in ("actions", "risk", "summary", "source_message")}
        try:
            with stages("approval"):
                created = await asyncio.to_thread(state.workflow.create, approval_id, request_id=rid, conv_id=conv_id, principal=principal,
                                                  action_requests=approval["action_requests"], summary=summary)
        except (PolicyViolation, ApprovalConflict, ValueError) as exc:
            # Tools validate first, so this is rare (e.g. policy changed mid-turn); never create a half-valid approval.
            logger.warning("approval not created for %s: %s", rid, exc)
            approval, approval_id = None, None
            reply += f"\n\n（审批申请未创建：{exc}）"
        else:
            metrics.APPROVALS.labels(event="created").inc()
            if created.get("superseded"):
                metrics.APPROVALS.labels(event="superseded").inc(len(created["superseded"]))
                reply += f"\n\n（本次申请已替代本会话中尚未审批的申请：{', '.join(created['superseded'])}）"
            if emit:
                await emit("approval_required", {"approval_id": approval_id, "approvals_required": created["approvals_required"],
                                                 "policy_version": created["policy_version"], "actions": approval["actions"]})

    flow.send("respond")
    response = ChatResponse(
        task_references=req.task_references,
        request_id=rid, response=reply, status=result.status, agent_type=result.agent_type.value,
        intent=result.intent.value if result.intent else "other", intent_group=req.intent_group or "other", entities=req.entities,
        confidence=req.intent_confidence, intent_confidence=req.intent_confidence, routing_confidence=result.routing_confidence,
        intent_source_scores=intent_result.source_scores, clarification_needed=bool(req.conversation_state.get("missing_fields")),
        missing_slots=req.conversation_state.get("missing_fields", []), inherited_from_previous=intent_result.inherited_from_previous, trace_id=result.trace_id,
        tools_used=result.tools_used, knowledge_used="search_knowledge_base" in result.tools_used or bool(req.knowledge), latency_ms=result.latency_ms,
        escalated=result.escalated, plan=result.plan, findings=result.findings, approval_required=bool(approval), approval_id=approval_id,
        token_usage=result.token_usage, stage_ms=stages.ms,
    )
    if state.memory:
        # commit 的耗时在写回之后才知道，只进入指标，不出现在本次响应和 trace 里。
        trace = _build_trace(result, intent_result, mem_ctx, approval, approval_id, principal, stages.ms)
        with stages("commit"):
            await state.memory.commit_turn(principal.scope_key, conv_id, rid, payload.message, response.model_dump(), trace, state=req.conversation_state)
        flow.send("commit")
        if state.profile_updater:
            state.profile_updater.submit(principal.scope_key, conv_id)
        if result.intent and result.intent.value not in {"greeting", "chitchat"}:
            metadata = {"intent": result.intent.value, "intent_group": req.intent_group or "other", "agent_type": result.agent_type.value, "entities": intent_result.entities}
            spawn(state.memory.store_episodic_turn(principal.scope_key, conv_id, rid, payload.message, response.response, metadata))
    metrics.CHAT_REQUESTS.labels(status=response.status, intent_group=response.intent_group).inc()
    metrics.CHAT_LATENCY.labels(status=response.status).observe(time.monotonic() - request_started)
    if state.monitor:
        state.monitor.record_chat(response.status, time.monotonic() - request_started)
    flow.send("finish")
    return response


def _sse(event: str, data: Any, event_id: Optional[str] = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _run_streamed(payload: ChatRequest, principal: Principal, rid: str, emit: EventPublisher) -> None:
    """后台执行一整轮并实时写入事件流。与 SSE 连接解耦：客户端断开不会取消执行，重连后续读即可。"""
    try:
        raw = await _chat_entry(payload, principal, rid, emit)
        response = raw if isinstance(raw, ChatResponse) else ChatResponse.model_validate(raw)
        # 正文是最终回答的分片（不是模型 token 流）；执行过程已经通过上面的事件实时推送。
        for index in range(0, len(response.response), 200):
            await emit("content_delta", {"delta": response.response[index:index + 200]})
        await emit("completed", response.model_dump())
    except HTTPException as exc:
        await emit("error", {"status_code": exc.status_code, "detail": exc.detail})
    except Exception:
        logger.exception("streamed chat failed")
        await emit("error", {"status_code": 500, "detail": "stream_failed"})
    finally:
        if emit.first_event_s is not None:
            metrics.STREAM_FIRST_EVENT.observe(emit.first_event_s)


async def _subscribe(stream: str, after: Optional[str]):
    """Relay a stream until its terminal event. A run that dies before writing one (process
    crash, deploy) would otherwise keep subscribers on heartbeats until the stream expires,
    so a stream quiet for longer than any single step can take is reported as abandoned.
    That verdict is the subscriber's only; it is not written to the stream, so a later
    resume still sees whatever the run publishes."""
    heartbeat_s = env_float("IT_COPILOT_SSE_HEARTBEAT_S", 15)
    idle_s = env_float("IT_COPILOT_SSE_IDLE_TIMEOUT_S", 300)
    last_event = time.monotonic()
    while True:
        events = await state.events.read(stream, after, block_s=heartbeat_s)
        if not events:
            if time.monotonic() - last_event >= idle_s:
                yield _sse("error", {"status_code": 504, "detail": "stream_abandoned", "retry_same_request": True})
                return
            yield ": heartbeat\n\n"
            continue
        last_event = time.monotonic()
        for event_id, event, data in events:
            after = event_id
            yield _sse(event, data, event_id)
            if event in TERMINAL_EVENTS:
                return


def _stream_response(stream: str, after: Optional[str]) -> StreamingResponse:
    return StreamingResponse(_subscribe(stream, after), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest, principal: Principal = Depends(current_principal)):
    """实时执行事件流：intent → routed/plan → tool_started/tool_finished（单 Agent 时穿插 answer_delta / answer_reset 实时正文）→ approval_required → content_delta → completed。

    每个事件带 id，可用 GET /chat/stream/{request_id} + Last-Event-ID 断线续传。相同 client_request_id 的
    重复提交只订阅已有执行，不会再跑一遍。"""
    if not state.orchestrator:
        raise HTTPException(503, "service not ready")
    rid = request_id(principal, payload.client_request_id)
    stream = "chat:" + rid
    try:
        claimed = await state.events.claim(stream, principal.scope_key, request_fingerprint(payload.message, payload.conv_id))
    except StreamConflict as exc:
        raise HTTPException(409, str(exc)) from None
    if claimed:
        emit = EventPublisher(state.events, stream, time.monotonic())
        await emit("status", {"stage": "started", "request_id": rid})
        spawn(_run_streamed(payload, principal, rid, emit))
    elif await state.events.owner(stream) != principal.scope_key:
        raise forbid()
    return _stream_response(stream, None)


@router.get("/chat/stream/{request_id}")
async def chat_stream_resume(request_id: str, principal: Principal = Depends(current_principal),
                             last_event_id: Optional[str] = Header(None, alias="Last-Event-ID")):
    stream = "chat:" + request_id
    owner = await state.events.owner(stream)
    if owner is None:
        raise HTTPException(404, "stream not found or expired")
    if owner != principal.scope_key:
        raise forbid()
    return _stream_response(stream, last_event_id)


async def _channel_turn(principal: Principal, text: str, conv_id: str, client_request_id: str) -> Dict[str, Any]:
    """IM 消息与 /chat 走同一条路径；client_request_id 取平台消息 ID，崩溃后重放返回已存回执。"""
    payload = ChatRequest(message=text[:8000], conv_id=conv_id[:200], client_request_id=client_request_id[:200])
    raw = await _chat_entry(payload, principal, request_id(principal, payload.client_request_id))
    return raw.model_dump() if isinstance(raw, ChatResponse) else dict(raw)
