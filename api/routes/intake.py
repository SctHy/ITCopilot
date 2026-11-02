"""外部入口：飞书事件回调、Alertmanager 告警与主动受理记录。"""
import asyncio
import hmac
import os
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

from api import state
from api.deps import require_roles
from core.identity import ROLE_AUDITOR, ROLE_IT_ADMIN, Principal
from monitor import metrics

router = APIRouter()


@router.post("/channels/feishu/events")
async def feishu_events(request: Request):
    """飞书事件订阅回调。校验 token/签名后只做落库，3 秒内返回；对话在后台处理。"""
    from intake.feishu import EventRejected, open_event, parse_message

    if not state.channels or not state.feishu:
        raise HTTPException(404, "feishu channel not configured")
    body = await request.body()
    try:
        event = open_event(state.feishu.settings, body, {k.lower(): v for k, v in request.headers.items()})
    except EventRejected as exc:
        metrics.AUTH_FAILURES.labels(reason="channel_event").inc()
        raise HTTPException(401, str(exc)) from exc
    if event.get("type") == "url_verification":
        return {"challenge": event.get("challenge")}
    message, reason = parse_message(state.feishu.settings, event)
    if message is None:
        metrics.CHANNEL_MESSAGES.labels(channel="feishu", outcome="filtered").inc()
        return {"ok": True, "ignored": reason}
    return {"ok": True, "outcome": await asyncio.to_thread(state.channels.accept, message)}


@router.post("/proactive/alerts")
async def proactive_alerts(request: Request, authorization: Optional[str] = Header(None)):
    """Alertmanager webhook（v4）：按 config/alert_rules.yaml 判定是否主动建单；观察模式只记录。"""
    secret = os.getenv("IT_COPILOT_ALERT_TOKEN", "")
    if not secret or not state.proactive:
        raise HTTPException(404, "proactive intake not configured")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip().encode(), secret.encode()):
        metrics.AUTH_FAILURES.labels(reason="alert_webhook").inc()
        raise HTTPException(401, "invalid alert webhook token", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(422, "body is not JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(422, "expected an Alertmanager webhook payload")
    decisions = await state.proactive.ingest(payload)
    return {"decisions": [{k: d[k] for k in ("id", "decision", "reason", "rule_id", "approval_id")} for d in decisions]}


@router.get("/proactive/decisions")
async def proactive_decisions(decision: Optional[str] = Query(None, pattern="^(accept|would_accept|ignore|off|resolved)$"),
                              limit: int = Query(50, ge=1, le=200), principal: Principal = Depends(require_roles(ROLE_IT_ADMIN, ROLE_AUDITOR))):
    """主动受理记录：观察模式下用 decision=would_accept 复核“本来会建的单”，确认无误再切到 auto。"""
    if not state.proactive:
        raise HTTPException(503, "proactive intake unavailable")
    return await asyncio.to_thread(state.proactive.list, decision, limit)
