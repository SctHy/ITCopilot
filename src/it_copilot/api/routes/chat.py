"""对话：同步 /chat、实时事件流 /chat/stream，以及 IM 渠道复用的同一条对话路径。"""
import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse

from it_copilot.agent.types import OrchestratorResult, Request
from it_copilot.api.container import Container, AppContainer
from it_copilot.api.deps import current_principal, enforce_rate_limit, forbid, request_id, spawn
from it_copilot.api.schemas import ChatRequest, ChatResponse
from it_copilot.infra.env import env_float
from it_copilot.infra.rate_limit import AGENT_TURN
from it_copilot.infra.events import TERMINAL_EVENTS, EventPublisher, StreamConflict, request_fingerprint
from it_copilot.infra.identity import Principal
from it_copilot.infra.monitor import metrics
from it_copilot.workflow.policy import PolicyViolation
from it_copilot.workflow.store import ApprovalConflict, ApprovalStateConflict

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


# 有草稿待确认时，这两类短回复由代码直接处理，不交给模型：确认与否是申请人的决定，模型只会再起草一遍。
CONFIRM_REPLY = re.compile(r"^\s*(?:好的?[，,\s]*)?(?:确认|确认[，,\s]*提交|确认申请|确认锁定|提交|提交审批|确定|确定提交|对[，,\s]*就这样提交吧|没问题[，,\s]*提交|confirm)\s*[。.!！]*\s*$", re.I)
CANCEL_REPLY = re.compile(r"^\s*(?:取消|取消申请|不提交了?|不用提交了?|放弃申请|cancel)\s*[。.!！]*\s*$", re.I)


def _confirmation_verb(message: str) -> Optional[str]:
    return "confirm" if CONFIRM_REPLY.match(message or "") else "cancel" if CANCEL_REPLY.match(message or "") else None


async def _answer_drafts(state: AppContainer, payload: ChatRequest, principal: Principal, rid: str, conv_id: str, verb: str, emit=None) -> Optional[ChatResponse]:
    """申请人回复“确认/取消”且本会话有待确认的草稿：直接确认或取消，返回确定性的回答；没有草稿时返回 None，照常走 Agent。"""
    drafts = await state.workflow.run(state.workflow.drafts, principal, conv_id)
    if payload.message.strip() == "确认锁定":
        drafts = [d for d in drafts if any(a["tool_name"] == "lock_device" for a in d["action_requests"])]
    if not drafts:
        if verb == "confirm":
            current = await state.workflow.run(state.workflow.conversation_approvals, principal, conv_id)
            if payload.message.strip() == "确认锁定":
                current = [a for a in current if "lock_device" in a["actions"]]
            pending = [a for a in current if a["status"] == "pending"]
            if pending:
                text = "\n".join(f"申请已提交，等待审批，无需重复提交：{a['summary']}。" for a in pending)
                return ChatResponse(request_id=rid, response=text, status="success", intent="other",
                                    intent_group="other", approval_required=True,
                                    approval_id=pending[0]["approval_id"], tools_used=[])
        return None
    started = time.monotonic()
    lines, last = [], None
    for draft in drafts:
        try:
            if verb == "confirm":
                last = await state.workflow.run(state.workflow.confirm, draft["approval_id"], principal)
            else:
                last = await state.workflow.run(state.workflow.withdraw, draft["approval_id"], principal, "申请人在对话中取消草稿")
        except (ApprovalStateConflict, ApprovalConflict, ValueError) as exc:
            lines.append(f"申请 {draft['approval_id']} 未能{'提交' if verb == 'confirm' else '取消'}：{exc}")
            continue
        status = (last or {}).get("status")
        if verb == "confirm" and status == "pending":
            metrics.APPROVALS.labels(event="confirmed").inc()
            lines.append(f"已确认提交：{draft['description']}，需 {last['approvals_required']} 人批准。")
            if emit:
                await emit("approval_required", {"approval_id": last["approval_id"], "approvals_required": last["approvals_required"],
                                                 "policy_version": last["policy_version"], "actions": last.get("actions", [])})
        elif verb == "cancel" and status == "withdrawn":
            metrics.APPROVALS.labels(event="withdrawn").inc()
            lines.append(f"已取消申请草稿，不会提交审批：{draft['description']}。")
        else:
            lines.append(f"申请 {draft['approval_id']} 当前状态为 {status}，未做改动。")
    submitted = verb == "confirm" and bool(last) and last.get("status") == "pending"
    response = ChatResponse(request_id=rid, response="\n".join(lines), status="success", intent="other", intent_group="other",
                            approval_required=submitted, approval_id=last["approval_id"] if submitted else None,
                            latency_ms=round((time.monotonic() - started) * 1000, 1), tools_used=[])
    if state.memory:
        await state.memory.commit_turn(principal.scope_key, conv_id, rid, payload.message, response.model_dump(),
                                       {"requester_reply": verb, "drafts": [d["approval_id"] for d in drafts], "owner": principal.subject})
    return response


async def _prefetch_knowledge(state: AppContainer, message: str, department):
    """预取只是加速手段：检索出错时当作没有结果，不能拖垮整轮（Agent 仍可自己调用检索工具）。"""
    if not state.tool_manager:
        return None
    try:
        # Explicitly separated questions need separate recall windows; otherwise the
        # first topic can crowd the second topic's current SOP out of the top three.
        queries = [part.strip(" ，,。；;？?") for part in re.split(r"(?:另外|顺便|同时)", message)]
        queries = [part for part in queries if part][:3] or [message]
        results = await asyncio.gather(*(state.tool_manager.search(
            "knowledge_search", query, top_k=3, department=department) for query in queries))
        good = [result for result in results if result.success]
        if not good:
            return results[0]
        from dataclasses import replace
        hits = {}
        for result in good:
            for hit in result.data:
                key = (hit.get("document_id"), hit.get("chunk"), hit.get("content"))
                hits.setdefault(key, hit)
        return replace(good[0], data=list(hits.values()))
    except Exception as exc:
        logger.warning("knowledge prefetch failed: %s", type(exc).__name__)
        return None


async def _run_turn(state: AppContainer, payload: ChatRequest, principal: Principal, rid: str, conv_id: str, stages: _Stages, emit=None):
    """一次对话回合：读取记忆 → 意图识别 → 组装请求 → 编排执行。用户与部门取自已验证身份。"""
    from it_copilot.agent.conversation_state import known_entities, merge_state, with_login_employee
    from it_copilot.agent.tools import slim_hit

    user_id = principal.subject
    with stages("memory_context"):
        mem_ctx = await state.memory.get_context(principal.scope_key, conv_id, query=payload.message) if state.memory else None
    history = mem_ctx.history_turns() if mem_ctx and mem_ctx.recent_messages else None
    conv_state = (getattr(mem_ctx, "state", {}) or {}) if mem_ctx else {}
    intent_history = list(history or [])
    if mem_ctx and mem_ctx.summary:
        intent_history.insert(0, {"role": "user", "content": "[较早会话摘要，仅用于解析指代，不代表本轮授权]\n" + mem_ctx.summary})
    with stages("intent"):
        # 知识库预取与意图识别并行：本地检索几十毫秒，用不用由意图识别判断，省掉 Agent 专门检索的那一轮。
        intent_result, prefetched = await asyncio.gather(
            state.orchestrator.recognize_intent(payload.message, history=intent_history, previous_intent_group=conv_state.get("intent_group")),
            _prefetch_knowledge(state, payload.message, principal.department),
        )
    if emit:
        await emit("intent", {"intent": intent_result.intent.value, "intent_group": intent_result.intent_group,
                              "confidence": round(intent_result.confidence, 4), "source": intent_result.recognition_source})

    req = Request(
        message=payload.message, user_id=user_id, conv_id=conv_id, context=mem_ctx.to_prompt_text(include_recent=False) if mem_ctx else "",
        history=history, entities=intent_result.entities, intent=intent_result.intent, intent_group=intent_result.intent_group,
        urgency=intent_result.urgency, intent_confidence=intent_result.confidence, department=principal.department, request_id=rid,
        principal=principal, emit=emit, allowed_writes=intent_result.requested_actions, intent_source=intent_result.recognition_source,
        knowledge=[slim_hit(x) for x in prefetched.data] if intent_result.needs_knowledge is not False and prefetched and prefetched.success else [],
    )
    req.context += f"\n[当前登录员工]\n工号 {principal.employee_id or '未绑定'}；部门 {principal.department}"
    async def read_request_context():
        context = ""
        if state.request_recall:
            try:
                req.related_approvals = await asyncio.wait_for(state.request_recall.recall(principal, payload.message, intent_result.intent.value), 3)
                context += state.request_recall.prompt(req.related_approvals)
            except Exception:
                logger.warning('request recall unavailable; continuing without it')
        return context

    async def read_approval_context():
        context = ""
        # Card actions bypass chat memory. Re-read persisted state on every turn, including
        # short follow-ups such as "好了吗", and retain terminal states as well as drafts.
        if state.workflow:
            try:
                current = await asyncio.wait_for(
                    state.workflow.run(state.workflow.conversation_approvals, principal, conv_id), 3)
                req.conversation_approvals = current
                from it_copilot.agent.request_recall import approval_state_prompt
                context += approval_state_prompt(current)
            except Exception:
                logger.warning("conversation approval state unavailable")
                context += "\n[申请状态查询失败]无法核实最新状态；不得声称已查询、尚未提交或要求重复提交。"
        return context

    req.context += "".join(await asyncio.gather(read_request_context(), read_approval_context()))
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
        },
        "intent": {"source": intent_result.recognition_source, "confidence": intent_result.confidence},
        "approval": {"required": bool(approval), "approval_id": approval_id},
        "fallback": {"intent_source": intent_result.recognition_source},
        "stage_ms": stage_ms,
        "versions": result.observability,
    }
    return trace


@router.post("/chat", response_model=ChatResponse)
async def chat(state: Container, payload: ChatRequest, principal: Principal = Depends(current_principal)):
    await enforce_rate_limit(state, principal, AGENT_TURN)
    return await _chat_entry(state, payload, principal, request_id(principal, payload.client_request_id))


async def _chat_entry(state: AppContainer, payload: ChatRequest, principal: Principal, rid: str, emit=None, channel="web"):
    if not state.orchestrator:
        raise HTTPException(503, "service not ready")
    from it_copilot.infra.monitor.telemetry import chat_span
    with chat_span(rid) as span:
        response = await _chat(state, payload, principal, rid, emit, channel)
        if isinstance(response, ChatResponse):
            for key in ("status", "intent", "intent_group"):
                span.set_attribute(f"it_copilot.{key}", getattr(response, key))
        return response


async def _chat(state: AppContainer, payload: ChatRequest, principal: Principal, rid: str, emit=None, channel="web"):
    request_started = time.monotonic()
    conv_id = payload.conv_id
    if state.memory:
        async with state.memory.conversation(principal.scope_key, conv_id):
            receipt = await state.memory.get_turn_receipt(principal.scope_key, conv_id, rid)
            if receipt:
                if receipt["message_hash"] != hashlib.sha256(payload.message.encode()).hexdigest():
                    raise HTTPException(409, "同一 client_request_id 不能用于不同消息")
                return receipt.get("response")
            return await _complete_turn(state, payload, principal, rid, conv_id, request_started, emit, channel)
    return await _complete_turn(state, payload, principal, rid, conv_id, request_started, emit, channel)


async def _record_turn(state: AppContainer, req, result, principal, channel, *, approval_id, stage_ms=None):
    from it_copilot.infra.turn_records import build_record, record_turn
    from it_copilot.infra.monitor.telemetry import turn_attributes
    record = build_record(req, result, channel=channel,
        policy_version=state.workflow.policy.version,
        kb_version=await asyncio.to_thread(state.kb.collection_version, principal.tenant) if state.kb else 0,
        approval_id=approval_id, stage_ms=stage_ms)

    def persist():
        with state.workflow.engine.begin() as db:
            record_turn(db, record)
    await state.workflow.run(persist)
    turn_attributes(record)


async def _record_history(state: AppContainer, principal: Principal, conv_id, rid, channel, message: str, response: ChatResponse) -> None:
    """Append the settled turn to the user's own web chat history (core/conversation_history.py).
    History is a convenience copy: failing to write it is logged and never fails the turn."""
    if channel != "web" or not conv_id or not state.workflow:
        return
    from it_copilot.infra.conversation_history import record_exchange

    def persist():
        with state.workflow.engine.begin() as db:
            record_exchange(db, principal, conv_id, rid, channel, message, response.model_dump())
    try:
        await state.workflow.run(persist)
    except Exception:
        logger.exception("chat history not recorded for %s", rid)


async def _complete_turn(state: AppContainer, payload, principal: Principal, rid, conv_id, request_started, emit=None, channel="web"):
    from it_copilot.agent.masking.model_masking import masking_scope

    with masking_scope(principal.scope_key, conv_id):
        return await _complete_turn_scoped(state, payload, principal, rid, conv_id, request_started, emit, channel)


async def _complete_turn_scoped(state: AppContainer, payload, principal: Principal, rid, conv_id, request_started, emit=None, channel="web"):
    # The caller holds the conversation lease through approval creation AND commit.
    stages = _Stages()
    verb = _confirmation_verb(payload.message) if state.workflow else None
    if verb:
        answered = await _answer_drafts(state, payload, principal, rid, conv_id, verb, emit)
        if answered is not None:
            # Confirmation turns never reach the model; record them so every settled turn has a row.
            req = Request(payload.message, principal=principal, conv_id=conv_id, request_id=rid, intent_source="confirmation",
                route_metadata={"model": "none", "route": "confirmation", "route_reason": "deterministic", "cost_usd": 0.0})
            if state.memory:
                req.conversation_state = await state.memory.get_state(principal.scope_key, conv_id)
            result = OrchestratorResult(rid, answered.response, None,
                latency_ms=answered.latency_ms, observability=state.orchestrator.agent._observability(req))
            await _record_turn(state, req, result, principal, channel, approval_id=answered.approval_id)
            await _record_history(state, principal, conv_id, rid, channel, payload.message, answered)
            return answered
    result, req, intent_result, mem_ctx = await _run_turn(state, payload, principal, rid, conv_id, stages, emit)

    from it_copilot.workflow.approval import approval_from_traces, provenance
    approval = approval_from_traces(result.tool_traces, payload.message)
    approval_id = rid + ":approval" if approval else None
    reply = result.response
    awaiting = False
    if approval and state.workflow:
        summary = {k: approval[k] for k in ("actions", "risk", "summary", "source_message")}
        try:
            with stages("approval"):
                created = await state.workflow.run(
                    state.workflow.create, approval_id, request_id=rid, conv_id=conv_id, principal=principal,
                    action_requests=approval["action_requests"], summary=summary, ask_requester=True,
                    provenance=provenance(result.tool_traces, request_id=rid, intent=intent_result, knowledge=req.knowledge,
                                          trace_id=result.trace_id, channel=channel))
        except (PolicyViolation, ApprovalConflict, ValueError) as exc:
            # Tools validate first, so this is rare (e.g. policy changed mid-turn); never create a half-valid approval.
            logger.warning("approval not created for %s: %s", rid, exc)
            approval, approval_id = None, None
            reply += f"\n\n（审批申请未创建：{exc}）"
        else:
            metrics.APPROVALS.labels(event="created").inc()
            draft = created["status"] == "draft"
            if created.get("superseded"):
                metrics.APPROVALS.labels(event="superseded").inc(len(created["superseded"]))
                replaced = "本会话中尚未确认的草稿" if draft else "本会话中尚未审批的申请"
                reply += f"\n\n（本次申请已替代{replaced}：{', '.join(created['superseded'])}）"
                if created.get("carried_over"):
                    reply += f"\n\n（被替代的申请里本次没有改动的事项已保留为单独的申请，仍按原内容等待{'确认' if draft else '审批'}：{', '.join(created['carried_over'])}）"
            if draft:
                awaiting = True
                if emit:
                    await emit("confirmation_required", {"approval_id": approval_id, "approvals_required": created["approvals_required"],
                                                         "policy_version": created["policy_version"], "actions": approval["actions"],
                                                         "expires_at": created["expires_at"]})
            elif emit:
                await emit("approval_required", {"approval_id": approval_id, "approvals_required": created["approvals_required"],
                                                 "policy_version": created["policy_version"], "actions": approval["actions"]})

    response = ChatResponse(
        related_approvals=req.related_approvals,
        request_id=rid, response=reply, status=result.status, error_code=result.error_code,
        intent=result.intent.value if result.intent else "other", intent_group=req.intent_group or "other", entities=req.entities,
        confidence=req.intent_confidence, intent_confidence=req.intent_confidence,
        intent_source_scores=intent_result.source_scores, clarification_needed=bool(req.conversation_state.get("missing_fields")),
        missing_slots=req.conversation_state.get("missing_fields", []), inherited_from_previous=intent_result.inherited_from_previous, trace_id=result.trace_id,
        tools_used=result.tools_used, knowledge_used="search_knowledge_base" in result.tools_used or bool(req.knowledge), latency_ms=result.latency_ms,
        escalated=result.escalated, approval_required=bool(approval), approval_id=approval_id,
        awaiting_confirmation=awaiting,
        token_usage=result.token_usage, stage_ms=stages.ms,
    )
    if state.workflow:
        await _record_turn(state, req, result, principal, channel, approval_id=approval_id, stage_ms=stages.ms)
        await _record_history(state, principal, conv_id, rid, channel, payload.message, response)
    if state.memory:
        # commit 的耗时在写回之后才知道，只进入指标，不出现在本次响应和 trace 里。
        trace = _build_trace(result, intent_result, mem_ctx, approval, approval_id, principal, stages.ms)
        trace["turn"] = {"user": payload.message, "assistant": response.response, "missing_slots": response.missing_slots}
        with stages("commit"):
            await state.memory.commit_turn(principal.scope_key, conv_id, rid, payload.message, response.model_dump(), trace, state=req.conversation_state)
        from it_copilot.agent.memory.admission import admit_turn
        episodic = admit_turn(response, intent_result, req.conversation_state)
        if episodic is not None:
            spawn(state, state.memory.store_episodic_turn(principal.scope_key, conv_id, rid, payload.message,
                                                   episodic.answer, episodic.metadata))
    metrics.CHAT_REQUESTS.labels(status=response.status, intent_group=response.intent_group).inc()
    metrics.CHAT_LATENCY.labels(status=response.status).observe(time.monotonic() - request_started)
    return response


def _sse(event: str, data: Any, event_id: Optional[str] = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _run_streamed(state: AppContainer, payload: ChatRequest, principal: Principal, rid: str, emit: EventPublisher) -> None:
    """后台执行一整轮并实时写入事件流。与 SSE 连接解耦：客户端断开不会取消执行，重连后续读即可。"""
    try:
        raw = await _chat_entry(state, payload, principal, rid, emit)
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


async def _subscribe(state: AppContainer, stream: str, after: Optional[str]):
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


def _stream_response(state: AppContainer, stream: str, after: Optional[str]) -> StreamingResponse:
    return StreamingResponse(_subscribe(state, stream, after), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/chat/stream")
async def chat_stream(state: Container, payload: ChatRequest, principal: Principal = Depends(current_principal)):
    """实时执行事件流：intent → tool_started/tool_finished（穿插 answer_delta / answer_reset 实时正文）→ approval_required → content_delta → completed。

    每个事件带 id，可用 GET /chat/stream/{request_id} + Last-Event-ID 断线续传。相同 client_request_id 的
    重复提交只订阅已有执行，不会再跑一遍。"""
    if not state.orchestrator:
        raise HTTPException(503, "service not ready")
    await enforce_rate_limit(state, principal, AGENT_TURN)
    rid = request_id(principal, payload.client_request_id)
    stream = "chat:" + rid
    try:
        claimed = await state.events.claim(stream, principal.scope_key, request_fingerprint(payload.message, payload.conv_id))
    except StreamConflict as exc:
        raise HTTPException(409, str(exc)) from None
    if claimed:
        emit = EventPublisher(state.events, stream, time.monotonic())
        await emit("status", {"stage": "started", "request_id": rid})
        spawn(state, _run_streamed(state, payload, principal, rid, emit))
    elif await state.events.owner(stream) != principal.scope_key:
        raise forbid()
    return _stream_response(state, stream, None)


@router.get("/chat/stream/{request_id}")
async def chat_stream_resume(state: Container, request_id: str, principal: Principal = Depends(current_principal),
                             last_event_id: Optional[str] = Header(None, alias="Last-Event-ID")):
    stream = "chat:" + request_id
    owner = await state.events.owner(stream)
    if owner is None:
        raise HTTPException(404, "stream not found or expired")
    if owner != principal.scope_key:
        raise forbid()
    return _stream_response(state, stream, last_event_id)
