"""Private web chat history: list, reopen and delete one's own past conversations.

Every read and write is keyed by the caller's (tenant, scope key); there is no admin read path,
so another user (including an IT admin) gets the same 404 as for a conversation that does not exist.

Bodies pass through ``servicedesk_agent.agent.masking.redaction.redact_text`` before they are stored (ADR 0032), so a password
typed into chat is kept as a placeholder, not as text. The retention sweep (``core/retention.py``)
removes messages and conversations older than ``SERVICEDESK_AGENT_RETENTION_HISTORY_DAYS``.

History is a record for the user, not Agent context: the Agent keeps reading short-lived working
memory, so continuing an old conversation shows its history while the model only recalls what
working memory still holds.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, delete, func, or_, select, update

from servicedesk_agent.infra import admin_audit
from servicedesk_agent.infra.db import conversation_messages, conversations, upsert
from servicedesk_agent.infra.identity import Principal
from servicedesk_agent.agent.masking.redaction import redact, redact_text

TITLE_CHARS = 60
# Reply fields kept so a reopened conversation shows the same summary line and approval cards.
ASSISTANT_META = ("status", "error_code", "intent", "intent_confidence", "clarification_needed", "missing_slots",
                  "approval_required", "approval_id", "awaiting_confirmation", "knowledge_used", "escalated",
                  "tools_used", "latency_ms")


def _title(text: str) -> str:
    title = " ".join(redact_text(text).split())
    return (title[:TITLE_CHARS - 1] + "…") if len(title) > TITLE_CHARS else (title or "新对话")


def record_exchange(db, principal: Principal, conv_id: str, request_id: str, channel: str,
                    message: str, response: Dict[str, Any], *, now: Optional[float] = None) -> bool:
    """Append one settled turn (user message + reply). Idempotent per request: a replay adds nothing."""
    now = time.time() if now is None else now
    owner, tenant = principal.scope_key, principal.tenant
    meta = redact({key: response.get(key) for key in ASSISTANT_META if response.get(key) not in (None, [], "")})
    rows = [dict(role="user", content=redact_text(message), meta={}, created_at=now),
            dict(role="assistant", content=redact_text(response.get("response") or ""), meta=meta, created_at=now)]
    insert = upsert(db, conversation_messages).values([
        dict(tenant=tenant, owner=owner, conv_id=conv_id, request_id=request_id, **row) for row in rows
    ])
    inserted = db.execute(insert.on_conflict_do_nothing(
        index_elements=["tenant", "owner", "request_id", "role"]
    ).returning(conversation_messages.c.id))
    added = len(inserted.all())
    if not added:
        return False
    head = upsert(db, conversations).values(tenant=tenant, owner=owner, conv_id=conv_id, channel=channel,
                                            title=_title(message), message_count=added, created_at=now, updated_at=now)
    db.execute(head.on_conflict_do_update(
        index_elements=["tenant", "owner", "conv_id"],
        set_={"message_count": conversations.c.message_count + added, "updated_at": now}))
    return True


def _mine(table, principal: Principal):
    return and_(table.c.tenant == principal.tenant, table.c.owner == principal.scope_key)


def list_conversations(db, principal: Principal, *, q: str = "", limit: int = 30,
                       before: Optional[float] = None) -> Dict[str, Any]:
    """Newest first. ``before`` is the ``updated_at`` of the last item already shown (keyset paging)."""
    condition = _mine(conversations, principal)
    if before is not None:
        condition = and_(condition, conversations.c.updated_at < before)
    if q.strip():
        pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        matches = select(conversation_messages.c.conv_id).where(
            _mine(conversation_messages, principal), conversation_messages.c.content.like(pattern, escape="\\"))
        condition = and_(condition, or_(conversations.c.title.like(pattern, escape="\\"), conversations.c.conv_id.in_(matches)))
    rows = db.execute(select(conversations).where(condition)
                      .order_by(conversations.c.updated_at.desc()).limit(limit + 1)).mappings().all()
    items = [_summary(row) for row in rows[:limit]]
    return {"items": items, "has_more": len(rows) > limit}


def _summary(row) -> Dict[str, Any]:
    return {"conv_id": row["conv_id"], "title": row["title"], "channel": row["channel"],
            "message_count": row["message_count"], "created_at": row["created_at"], "updated_at": row["updated_at"]}


def get_conversation(db, principal: Principal, conv_id: str) -> Optional[Dict[str, Any]]:
    head = db.execute(select(conversations).where(_mine(conversations, principal),
                                                  conversations.c.conv_id == conv_id)).mappings().first()
    if head is None:
        return None
    rows = db.execute(select(conversation_messages).where(
        _mine(conversation_messages, principal), conversation_messages.c.conv_id == conv_id)
        .order_by(conversation_messages.c.id)).mappings().all()
    messages: List[Dict[str, Any]] = [{"request_id": r["request_id"], "role": r["role"], "content": r["content"],
                                       "meta": r["meta"] or {}, "created_at": r["created_at"]} for r in rows]
    return {**_summary(head), "messages": messages}


def rename_conversation(db, principal: Principal, conv_id: str, title: str) -> bool:
    title = " ".join(redact_text(title).split())[:TITLE_CHARS]
    if not title:
        raise ValueError("标题不能为空")
    result = db.execute(update(conversations).where(_mine(conversations, principal), conversations.c.conv_id == conv_id)
                        .values(title=title))
    return bool(result.rowcount)


def delete_conversation(db, principal: Principal, conv_id: str) -> Optional[int]:
    """Delete one of the caller's conversations; None when it is not theirs (or does not exist).
    The audit entry commits in the same transaction."""
    exists = db.execute(select(func.count()).select_from(conversations).where(
        _mine(conversations, principal), conversations.c.conv_id == conv_id)).scalar_one()
    if not exists:
        return None
    removed = db.execute(delete(conversation_messages).where(
        _mine(conversation_messages, principal), conversation_messages.c.conv_id == conv_id)).rowcount or 0
    db.execute(delete(conversations).where(_mine(conversations, principal), conversations.c.conv_id == conv_id))
    admin_audit.append(db, tenant=principal.tenant, actor=principal.subject, event="chat_history_deleted",
                       subject=conv_id, detail={"messages": removed})
    return removed
