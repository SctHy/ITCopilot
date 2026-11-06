"""Channel service: IM messages in, chat turns, replies and approval notices out.

Every step is stored before it is done (after DeepSeek dsh-superagent's "stored work"):

- ``accept`` stores an addressed message in ``channel_inbox``; the primary key drops
  platform retries. Group chatter that does not address the bot is not stored at all.
- A pass claims due inbox rows with a lease, resolves the sender to a Principal, runs one
  chat turn through the same path as ``/chat`` (``client_request_id`` = message id, so a
  turn re-run after a crash returns the stored receipt) and, in one transaction, marks
  the row done and queues the reply in ``channel_outbox``.
- ``notify_approval`` is the ``WorkflowStore.notify`` hook: an approval that settles
  (rejected, expired, invalidated, executed) queues one notice to the conversation that
  asked for it, in the same transaction as the state change.
- Outgoing messages of one conversation go out in the order they were queued; a message
  waits behind an earlier one until that one has failed three times. Transient failures
  back off from 5 s to 300 s; a permanent one (message recalled, bot removed) drops the
  message and keeps the record.

Attention budget: a person sees a reaction on receipt, one answer, and one notice when
the approval settles. Nothing else is sent.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from core import db as schema
from core.db import channel_inbox, channel_outbox
from core.identity import Principal
from intake.base import ChannelError, InboundMessage, Sender
from monitor import metrics

logger = logging.getLogger(__name__)

# (principal, text, conv_id, client_request_id) -> ChatResponse as dict
TurnHandler = Callable[[Principal, str, str, str], Awaitable[Dict[str, Any]]]
Resolver = Callable[[Dict[str, Any]], Optional[Principal]]

UNBOUND = "你的账号还没有绑定 IT-Copilot 身份，暂时无法受理。请联系 IT 管理员开通。"
TEXT_ONLY = "目前只能处理文字消息，请用文字描述问题。"
TURN_FAILED = "这条消息处理失败了，请稍后重新发送。"
NOTICES = {
    "rejected": "审批单 {short} 未通过审批。",
    "expired": "审批单 {short} 超过有效期无人审批，已过期；如仍需要请重新提交。",
    "invalidated": "审批单 {short} 已失效：{reason}。如仍需要请按新规则重新申请。",
    "execution_succeeded": "审批单 {short} 已批准并执行完成{results}。",
    "execution_failed": "审批单 {short} 已批准，但执行失败：{error}",
    "execution_manual_review": "审批单 {short} 的执行结果需要 IT 人工核对，核对完成后会再通知你。",
}
BLOCKING_ATTEMPTS = 3   # an undelivered message stops holding back later ones after this many failures


def short_id(approval_id: str) -> str:
    return approval_id.split(":")[0][:8]


def _key(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()


def _results_text(results: List[Dict[str, Any]]) -> str:
    parts = []
    for r in results or []:
        if r.get("ticket_id"):
            parts.append(f"工单号 {r['ticket_id']}")
        elif r.get("action") == "request_access" and r.get("system"):
            parts.append(f"{r['system']} {r.get('role', '')} 权限已开通".replace("  ", " "))
    return "：" + "；".join(parts) if parts else ""


def format_reply(response: Dict[str, Any]) -> str:
    text = (response.get("response") or "").strip()
    if response.get("approval_required") and response.get("approval_id"):
        text += f"\n\n已提交审批（审批单 {short_id(response['approval_id'])}），审批结果会在这里通知你。"
    return text


def enqueue(db, *, now: float, channel: str, conv_key: str, address: Dict[str, Any], text: str, ref: str, key: str) -> None:
    """Queue a message inside the caller's transaction; the same key is queued once."""
    seq = (db.execute(select(func.max(channel_outbox.c.seq)).where(channel_outbox.c.conv_key == conv_key)).scalar() or 0) + 1
    try:
        with db.begin_nested():
            db.execute(insert(channel_outbox).values(
                id=key[:64], channel=channel, conv_key=conv_key, seq=seq, address=address, text=text, ref=ref, status="queued",
                attempts=0, not_before=now, version=0, created_at=now))
    except IntegrityError:
        return


def approval_notifier(channels: Iterable[str], clock: Callable[[], float] = time.time) -> Callable[[Any, Dict[str, Any]], None]:
    """``WorkflowStore.notify`` hook: tell the conversation that asked for an approval how it settled.

    Needs only the database, so every process that settles approvals (API, standalone
    worker) attaches it; the channel service in the API process sends what it queues."""
    channels = frozenset(channels)

    def notify(db, notice: Dict[str, Any]) -> None:
        conv_id = notice.get("conv_id") or ""
        channel = conv_id.split(":", 1)[0]
        template = NOTICES.get(notice["event"])
        if channel not in channels or not template:
            return
        origin = db.execute(select(channel_inbox.c.address).where(channel_inbox.c.request_id == notice["request_id"])).first() \
            or db.execute(select(channel_inbox.c.address).where(channel_inbox.c.conv_id == conv_id).order_by(channel_inbox.c.created_at.desc())).first()
        if not origin:
            return
        detail = notice.get("detail") or {}
        text = template.format(short=short_id(notice["approval_id"]), reason=detail.get("reason") or "", error=detail.get("error") or "未知原因",
                               results=_results_text(detail.get("results")))
        enqueue(db, now=clock(), channel=channel, conv_key=conv_id, address=origin.address, text=text, ref=notice["approval_id"],
                key=_key(notice["approval_id"], notice["event"]))

    return notify


class ChannelService:
    def __init__(self, engine: Engine, senders: Dict[str, Sender], handle: TurnHandler, resolvers: Dict[str, Resolver], *,
                 clock: Callable[[], float] = time.time, worker_id: Optional[str] = None, lease_s: float = 180.0,
                 poll_s: float = 1.0, concurrency: int = 4, max_turn_attempts: int = 3, max_send_attempts: int = 8):
        self.engine = engine
        self.senders = senders
        self.handle = handle
        self.resolvers = resolvers
        self.clock = clock
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self.lease_s = lease_s
        self.poll_s = poll_s
        self.concurrency = concurrency
        self.max_turn_attempts = max_turn_attempts
        self.max_send_attempts = max_send_attempts
        self.notify_approval = approval_notifier(senders, clock)
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="channel-service")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    def kick(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("channel service iteration failed")
                processed = 0
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_s)
            except TimeoutError:
                pass

    async def run_once(self) -> int:
        rows = await asyncio.to_thread(self._claim, channel_inbox, self.concurrency)
        if rows:
            await asyncio.gather(*(self._process(row) for row in rows))
        sent = 0
        for row in await asyncio.to_thread(self._claim, channel_outbox, 20):
            await self._deliver(row)
            sent += 1
        return len(rows) + sent

    async def run_until_idle(self, max_rounds: int = 50) -> None:
        for _ in range(max_rounds):
            if not await self.run_once():
                return

    # ── inbound ─────────────────────────────────────────────────────────────

    def accept(self, msg: InboundMessage) -> str:
        """Store an addressed message. Returns queued / duplicate / ignored."""
        now = self.clock()
        with self.engine.begin() as db:
            if not msg.addressed and not self._is_followup(db, msg):
                outcome = "ignored"
            else:
                try:
                    with db.begin_nested():
                        db.execute(insert(channel_inbox).values(
                            id=msg.id, channel=msg.channel, conv_id=msg.conv_id, sender=msg.sender, text=msg.text[:8000], address=msg.address,
                            status="received", attempts=0, not_before=now, version=0, created_at=now, updated_at=now))
                    outcome = "queued"
                except IntegrityError:
                    outcome = "duplicate"
        metrics.CHANNEL_MESSAGES.labels(channel=msg.channel, outcome=outcome).inc()
        if outcome == "queued":
            self.kick()
        return outcome

    @staticmethod
    def _is_followup(db, msg: InboundMessage) -> bool:
        """A reply in a thread the same person opened with the bot continues it without another @."""
        if not msg.thread_root:
            return False
        root = db.execute(select(channel_inbox.c.sender).where(channel_inbox.c.id == msg.thread_root)).scalar()
        return bool(root) and root.get("open_id") == msg.sender.get("open_id")

    def _claim(self, table, limit: int) -> List[Dict[str, Any]]:
        now = self.clock()
        idle = "received" if table is channel_inbox else "queued"
        busy = "processing" if table is channel_inbox else "sending"
        due = or_(and_(table.c.status == idle, table.c.not_before <= now), and_(table.c.status == busy, table.c.lease_until < now))
        with schema.read(self.engine) as db:
            candidates = db.execute(select(table).where(due).order_by(table.c.created_at).limit(limit * 4)).mappings().all()
        claimed, convs = [], set()
        for row in candidates:
            if len(claimed) >= limit:
                break
            conv = row["conv_id"] if table is channel_inbox else row["conv_key"]
            if conv in convs:
                continue  # one message per conversation at a time
            with self.engine.begin() as db:
                if table is channel_inbox and db.execute(select(channel_inbox.c.id).where(
                        channel_inbox.c.conv_id == conv, channel_inbox.c.status.in_(("received", "processing")), channel_inbox.c.id != row["id"],
                        or_(channel_inbox.c.created_at < row["created_at"],
                            and_(channel_inbox.c.created_at == row["created_at"], channel_inbox.c.id < row["id"])))).first():
                    continue  # an earlier message of this conversation is not answered yet
                if table is channel_outbox and db.execute(select(channel_outbox.c.id).where(
                        channel_outbox.c.conv_key == conv, channel_outbox.c.seq < row["seq"],
                        channel_outbox.c.status.in_(("queued", "sending")), channel_outbox.c.attempts < BLOCKING_ATTEMPTS)).first():
                    continue  # an earlier message to this conversation has not gone out yet
                done = db.execute(update(table).where(table.c.id == row["id"], table.c.version == row["version"]).values(
                    status=busy, lease_until=now + self.lease_s, attempts=row["attempts"] + 1, version=row["version"] + 1)).rowcount
            if done:
                convs.add(conv)
                claimed.append({**row, "status": busy, "attempts": row["attempts"] + 1, "version": row["version"] + 1})
        return claimed

    def _finish(self, row, status: str, *, reply: Optional[str] = None, request_id: Optional[str] = None, error: Optional[str] = None) -> None:
        now = self.clock()
        with self.engine.begin() as db:
            done = db.execute(update(channel_inbox).where(channel_inbox.c.id == row["id"], channel_inbox.c.version == row["version"]).values(
                status=status, request_id=request_id, error=error, lease_until=None, version=row["version"] + 1, updated_at=now)).rowcount
            if done and reply:
                self.enqueue(db, channel=row["channel"], conv_key=row["conv_id"], address=row["address"], text=reply,
                             ref=row["id"], key=_key(row["id"], "reply"))
        metrics.CHANNEL_MESSAGES.labels(channel=row["channel"], outcome=status).inc()

    def _retry_turn(self, row, error: str) -> None:
        delay = min(5 * 2 ** (row["attempts"] - 1), 300)
        with self.engine.begin() as db:
            db.execute(update(channel_inbox).where(channel_inbox.c.id == row["id"], channel_inbox.c.version == row["version"]).values(
                status="received", error=error[:500], not_before=self.clock() + delay, lease_until=None, version=row["version"] + 1,
                updated_at=self.clock()))

    async def _process(self, row: Dict[str, Any]) -> None:
        sender = self.senders.get(row["channel"])
        principal = self.resolvers[row["channel"]](row["sender"]) if row["channel"] in self.resolvers else None
        if principal is None:
            await asyncio.to_thread(self._finish, row, "ignored", reply=UNBOUND, error="unbound_sender")
            return
        if not row["text"]:
            await asyncio.to_thread(self._finish, row, "ignored", reply=TEXT_ONLY, error="no_text")
            return
        if sender and row["attempts"] == 1:
            try:
                await sender.acknowledge(row["address"])
            except Exception as exc:  # a missing reaction costs nothing
                logger.info("acknowledge failed for %s: %s", row["id"], exc)
        try:
            response = await self.handle(principal, row["text"], row["conv_id"], row["id"])
        except Exception as exc:
            logger.warning("channel turn %s failed (attempt %s): %s", row["id"], row["attempts"], exc, exc_info=True)
            detail = getattr(exc, "detail", None) or type(exc).__name__
            if row["attempts"] >= self.max_turn_attempts or getattr(exc, "status_code", 500) < 500:
                await asyncio.to_thread(self._finish, row, "failed", reply=TURN_FAILED, error=str(detail)[:500])
            else:
                await asyncio.to_thread(self._retry_turn, row, str(detail))
            return
        await asyncio.to_thread(self._finish, row, "done", reply=format_reply(response), request_id=response.get("request_id"))

    # ── outbound ────────────────────────────────────────────────────────────

    def enqueue(self, db, *, channel: str, conv_key: str, address: Dict[str, Any], text: str, ref: str, key: str) -> None:
        enqueue(db, now=self.clock(), channel=channel, conv_key=conv_key, address=address, text=text, ref=ref, key=key)

    def _settle(self, row, **values) -> None:
        with self.engine.begin() as db:
            db.execute(update(channel_outbox).where(channel_outbox.c.id == row["id"], channel_outbox.c.version == row["version"])
                       .values(lease_until=None, version=row["version"] + 1, **values))

    async def _deliver(self, row: Dict[str, Any]) -> None:
        sender = self.senders.get(row["channel"])
        try:
            if sender is None:
                raise ChannelError(f"渠道 {row['channel']} 未配置", permanent=False)
            await sender.send(row["address"], row["text"], row["id"])
        except Exception as exc:
            permanent = isinstance(exc, ChannelError) and exc.permanent
            if permanent:
                outcome, values = "dropped", {"status": "dropped"}
            elif row["attempts"] >= self.max_send_attempts:
                outcome, values = "failed", {"status": "failed"}
            else:
                outcome, values = "retry", {"status": "queued", "not_before": self.clock() + min(5 * 2 ** (row["attempts"] - 1), 300)}
            logger.warning("delivery %s %s: %s", row["id"], outcome, exc)
            await asyncio.to_thread(self._settle, row, error=str(exc)[:500], **values)
        else:
            outcome = "sent"
            await asyncio.to_thread(self._settle, row, status="sent", error=None, sent_at=self.clock())
        metrics.CHANNEL_DELIVERIES.labels(channel=row["channel"], outcome=outcome).inc()

    # ── read model ──────────────────────────────────────────────────────────

    def problems(self) -> Dict[str, Any]:
        """What needs a person: messages that failed or keep failing. Healthy is empty."""
        with schema.read(self.engine) as db:
            failed_in = db.execute(select(channel_inbox.c.id, channel_inbox.c.error).where(channel_inbox.c.status == "failed")
                                   .order_by(channel_inbox.c.updated_at.desc()).limit(20)).mappings().all()
            stuck_out = db.execute(select(channel_outbox.c.id, channel_outbox.c.ref, channel_outbox.c.status, channel_outbox.c.attempts,
                                          channel_outbox.c.error)
                                   .where(or_(channel_outbox.c.status == "failed",
                                              and_(channel_outbox.c.status.in_(("queued", "sending")), channel_outbox.c.attempts >= BLOCKING_ATTEMPTS)))
                                   .order_by(channel_outbox.c.created_at.desc()).limit(20)).mappings().all()
        return {"failed_messages": [dict(r) for r in failed_in], "undelivered": [dict(r) for r in stuck_out]}
