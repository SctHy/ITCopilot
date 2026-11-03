"""外部入口：飞书事件回调。"""
import asyncio

from fastapi import APIRouter, HTTPException, Request

from api import state
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

