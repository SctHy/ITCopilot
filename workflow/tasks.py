"""Shared IT matters: ordered transcript, authorization, and durable Agent work.

The bare store permits requesters and tenant IT admins. An injected access policy
adds explicitly invited/assigned members. The model never controls membership,
votes, or business execution. A run and
its reply/approval commit atomically; only its current unexpired lease may commit.
"""
import hashlib
import json
import time
from contextlib import contextmanager
import uuid

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from core import db as schema
from core.db import service_tasks as tasks, task_events as events, task_runs as runs
from core.concurrency import LeaseLostError
from workflow.task_ports import NoTaskEffects, TaskEffects


def key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


class TaskForbidden(ValueError):
    pass


class TaskConflict(ValueError):
    pass


class TaskStore:
    def __init__(self, engine, *, effects: TaskEffects | None = None, clock=time.time):
        self.engine, self.clock = engine, clock
        self.effects = effects if effects is not None else NoTaskEffects()
        self.governance = None
        self.on_status = None
        self.access = None

    @contextmanager
    def transaction(self, task_id, principal=None, *, lock=True, write=True):
        """Integration port: authorized task row and a shared atomic transaction.

        Omitting principal is only for trusted background adapters.
        """
        with self.engine.begin() as db:
            if lock and not write and principal is not None:
                self._require(db, task_id, principal)
                self._require(db, task_id, lock=True)
                yield db, self._require(db, task_id, principal)
            else:
                yield db, self._require(db, task_id, principal, lock=lock)

    @contextmanager
    def read(self, task_id, principal=None):
        """Read-only port: authorized task row plus a consistent snapshot, without writes.

        SQLite (WAL, deferred BEGIN) pins one snapshot for the whole connection. PostgreSQL
        takes FOR SHARE on the task row, so membership changes (which UPDATE that row) wait
        until this read finishes, while concurrent readers do not block each other.
        """
        conditions = [tasks.c.id == task_id]
        if principal is not None:
            conditions.append(self.access_filter(principal))
        with schema.read(self.engine) as db:
            row = db.execute(select(tasks).where(*conditions).with_for_update(read=True, of=tasks)).mappings().first()
            if not row:
                raise TaskForbidden("事项不存在或当前身份无权访问")
            yield db, dict(row)

    def record(self, db, task, principal, kind, text, data=None, *, wake=False, event_id=None):
        return self._append(db, task, event_id or uuid.uuid4().hex, kind,
            principal.subject if principal else 'system:tasks', principal.username if principal else '任务服务',
            text, data, wake=wake)

    def set_status(self, db, task, status):
        db.execute(update(tasks).where(tasks.c.id == task['id']).values(status=status))
        task['status'] = status
        if status != 'open':
            db.execute(update(runs).where(runs.c.task_id == task['id'], runs.c.status.in_(['queued', 'running', 'held']))
                       .values(status='cancelled', version=runs.c.version + 1, lease_until=None))
        if self.on_status:
            self.on_status(db, task, status)

    def access_filter(self, principal):
        if self.access:
            return self.access.filter(principal)
        scope = tasks.c.tenant == principal.tenant
        if principal.is_admin:
            return scope
        return and_(scope, tasks.c.requester == principal.subject)

    def _require(self, db, task_id, principal=None, *, lock=False):
        conditions = [tasks.c.id == task_id]
        if principal is not None:
            conditions.append(self.access_filter(principal))
        if lock:
            # Atomic UPDATE serializes appends/claims on both SQLite and PostgreSQL.
            changed = db.execute(update(tasks).where(*conditions).values(version=tasks.c.version + 1)).rowcount
            if not changed:
                raise TaskForbidden("事项不存在或当前身份无权访问")
        row = db.execute(select(tasks).where(*conditions)).mappings().first()
        if not row:
            raise TaskForbidden("事项不存在或当前身份无权访问")
        if lock and principal is not None and self.access and not self.access.writable(db, dict(row), principal):
            raise TaskForbidden('观察者不能修改事项')
        return dict(row)

    def _append(self, db, task, event_id, kind, actor, name, text, data=None, *, wake=False):
        old = db.execute(select(events).where(events.c.id == event_id)).mappings().first()
        if old:
            return dict(old)
        seq = task["version"] + 1
        task["version"] = seq
        now = self.clock()
        db.execute(update(tasks).where(tasks.c.id == task["id"]).values(version=seq, updated_at=now))
        record = dict(id=event_id, task_id=task["id"], seq=seq, kind=kind, actor=actor,
                      actor_name=name, text=text, data=data or {}, created_at=now)
        db.execute(insert(events).values(**record))
        if wake and task["status"] == "open":
            db.execute(insert(runs).values(id=event_id, task_id=task["id"], seq=seq, status="queued",
                                          attempts=0, version=0, not_before=now))
        return record

    def create(self, principal, title, message, client_id):
        task_id = key("task", principal.tenant, principal.subject, client_id)
        try:
            with self.engine.begin() as db:
                old = db.execute(select(tasks).where(tasks.c.id == task_id)).mappings().first()
                if not old:
                    now = self.clock()
                    record = dict(id=task_id, title=title, requester=principal.subject, principal=principal.public(),
                                  tenant=principal.tenant, department=principal.department, status="open",
                                  version=0, created_at=now, updated_at=now)
                    db.execute(insert(tasks).values(**record))
                    self._append(db, record, key(task_id, "initial"), "requester", principal.subject, principal.username,
                                 message, {"principal": principal.public()}, wake=True)
        except IntegrityError:
            # Concurrent replay of the same create; validate its payload below.
            pass
        body = self.get(task_id, principal)
        if body["title"] != title or body["events"][0]["text"] != message:
            raise TaskConflict("同一请求键不能创建不同事项")
        return body

    def post(self, task_id, principal, message, client_id, ask_agent=True):
        with self.engine.begin() as db:
            task = self._require(db, task_id, principal, lock=True)
            event_id = key(task_id, principal.subject, client_id)
            old = db.execute(select(events).where(events.c.id == event_id)).mappings().first()
            if old:
                if old["text"] != message or old["data"].get("ask_agent") != ask_agent:
                    raise TaskConflict("同一请求键不能用于不同消息")
                return self._public_event(old)
            if task["status"] != "open":
                raise TaskConflict("事项未打开，请重新打开后继续")
            owner = task["requester"] == principal.subject
            kind = "requester" if owner else "approver" if principal.has_any('approver', 'it_admin') else "collaborator"
            event = self._append(db, task, event_id, kind, principal.subject,
                                 principal.username, message, {"principal": principal.public(), "ask_agent": ask_agent}, wake=ask_agent)
            return self._public_event(event)

    @staticmethod
    def _public_event(row):
        return {**dict(row), "data": {k: v for k, v in row["data"].items() if k != "principal"}}

    def get(self, task_id, principal, event_limit=None):
        with schema.read(self.engine) as db:
            task = self._require(db, task_id, principal)
            query = select(events).where(events.c.task_id == task_id)
            query = query.order_by(events.c.seq.desc()).limit(event_limit + 1) if event_limit else query.order_by(events.c.seq)
            transcript = db.execute(query).mappings().all()
            has_more = bool(event_limit and len(transcript) > event_limit)
            if event_limit:
                transcript = list(reversed(transcript[:event_limit]))
            can_write = self.access.writable(db, task, principal) if self.access else True
            jobs = db.execute(select(runs).where(runs.c.task_id == task_id).order_by(runs.c.seq)).mappings().all()
        extra = self.effects.view(task)
        if self.access:
            assigned = self.access.reviewers(task_id, principal)
            for approval in extra.get('approvals', []):
                approval['assigned_reviewers'] = assigned
                approval['room_can_write'] = can_write
        busy = [j for j in jobs if j["status"] in {"queued", "running"}]
        phase = task["status"] if task["status"] != "open" else (
            "working" if busy else "budget_paused" if any(j["status"] == "held" for j in jobs) else "failed" if jobs and jobs[-1]["status"] == "failed" else extra.get("phase", "waiting"))
        return {k: v for k, v in task.items() if k != "principal"} | {
            **extra, "requester_name": task["principal"]["username"], "phase": phase,
            "events": [self._public_event(e) for e in transcript], "has_more_events": has_more, "can_write": can_write,
            "runs": [{k: j[k] for k in ("id", "seq", "status", "attempts", "error")} for j in jobs],
        }

    def list(self, principal):
        with schema.read(self.engine) as db:
            records = db.execute(select(tasks).where(self.access_filter(principal)).order_by(tasks.c.updated_at.desc()).limit(100)).mappings().all()
        return [{k: v for k, v in r.items() if k != "principal"} | {"requester_name": r["principal"]["username"]} for r in records]

    def stop(self, task_id, principal):
        with self.engine.begin() as db:
            task = self._require(db, task_id, principal, lock=True)
            if task["requester"] != principal.subject and not principal.is_admin:
                raise TaskForbidden("只有申请人或 IT 管理员可以停止跟进")
            if task['status'] == 'open':
                self.set_status(db, task, 'stopped')
                self._append(db, task, key(task_id, "stopped", task['version']), "system", principal.subject, principal.username,
                             "已停止 Agent 跟进。现有审批和已批准的业务动作仍按原工作流处理。")
        return self.get(task_id, principal)

    def retry(self, task_id, run_id, principal):
        with self.engine.begin() as db:
            task = self._require(db, task_id, principal, lock=True)
            if task["status"] != "open":
                raise TaskConflict("已停止的事项不能重试")
            old = db.execute(select(runs).where(runs.c.id == run_id, runs.c.task_id == task_id)).mappings().first()
            if not old or db.execute(select(runs.c.id).where(runs.c.task_id == task_id, runs.c.seq > old["seq"],
                                                            runs.c.status.in_(("running", "done")))).first():
                raise TaskConflict("已有更新的处理结果，请发送新消息继续事项")
            changed = db.execute(update(runs).where(runs.c.task_id == task_id, runs.c.id == run_id, runs.c.status == "failed")
                                 .values(status="queued", attempts=0, version=runs.c.version + 1, not_before=self.clock(), error=None)).rowcount
            if not changed:
                raise TaskConflict("只有失败的 Agent 回合可以重试")
            self._append(db, task, key(task_id, "retry", run_id, task["version"]), "system", principal.subject,
                         principal.username, "重新排队失败的 Agent 回合。")

    def ingest(self, task_id, *, source, object_id, version, kind, text, data=None, wake=True,
               actor=None, actor_name=None, event_id=None):
        """Internal event port, not an HTTP permission bypass. Sources are trusted adapters.

        The source object/version is a durable cursor: duplicate or out-of-order snapshots
        cannot wake a task again, even after a restart or concurrent delivery.
        Stopped tasks still record outcomes; _append suppresses new Agent runs for them.
        """
        with self.engine.begin() as db:
            event_id = event_id or key(task_id, source, object_id, version)
            if db.execute(select(events.c.id).where(events.c.id == event_id)).first():
                return False
            task = self._require(db, task_id, lock=True)
            prior = db.execute(select(events.c.data).where(events.c.task_id == task_id).order_by(events.c.seq.desc())).scalars()
            for item in prior:
                cursor = item.get("source_cursor", {})
                if cursor.get("source") == source and cursor.get("object_id") == object_id:
                    if cursor["version"] >= version:
                        return False
                    break
            self._append(db, task, event_id, kind, actor or "system:" + source,
                         actor_name or source, text,
                         {**(data or {}), "source_cursor": {"source": source, "object_id": object_id, "version": version}}, wake=wake)
            return True

    def claim(self, lease_s=120):
        now = self.clock()
        due = or_(and_(runs.c.status == "queued", runs.c.not_before <= now),
                  and_(runs.c.status == "running", runs.c.lease_until <= now))
        earlier = runs.alias("earlier_task_run")
        blocked = select(earlier.c.id).where(earlier.c.task_id == runs.c.task_id, earlier.c.id != runs.c.id,
            or_(and_(earlier.c.seq < runs.c.seq, earlier.c.status.in_(("queued", "running", "held"))),
                earlier.c.status == "running")).exists()
        with self.engine.begin() as db:
            candidates = db.execute(select(runs).join(tasks, tasks.c.id == runs.c.task_id).where(due, ~blocked, tasks.c.status == "open")
                                    .order_by(runs.c.not_before, runs.c.seq).limit(32)).mappings().all()
            for candidate in candidates:
                task = self._require(db, candidate["task_id"], lock=True)
                if task["status"] != "open":
                    continue
                prior = db.execute(select(runs.c.id).where(runs.c.task_id == task["id"], runs.c.id != candidate["id"],
                    or_(and_(runs.c.seq < candidate["seq"], runs.c.status.in_(("queued", "running", "held"))), runs.c.status == "running"))).first()
                if prior:
                    continue
                changed = db.execute(update(runs).where(runs.c.id == candidate["id"], runs.c.version == candidate["version"], due)
                                     .values(status="running", attempts=candidate["attempts"] + 1,
                                             version=candidate["version"] + 1, lease_until=now + lease_s)).rowcount
                if changed:
                    claim = {**candidate, "status": "running", "attempts": candidate["attempts"] + 1,
                             "version": candidate["version"] + 1, "lease_until": now + lease_s, "tenant": task["tenant"]}
                    if self.governance and not self.governance.admit(db, task, claim):
                        continue
                    return claim
        return None

    def _owned(self, claim):
        return and_(runs.c.id == claim["id"], runs.c.version == claim["version"],
                    runs.c.status == "running", runs.c.lease_until > self.clock())

    def renew(self, claim, lease_s):
        with self.engine.begin() as db:
            return bool(db.execute(update(runs).where(self._owned(claim)).values(lease_until=self.clock() + lease_s)).rowcount)

    def context(self, claim):
        with schema.read(self.engine) as db:
            task = self._require(db, claim["task_id"])
            event = dict(db.execute(select(events).where(events.c.id == claim["id"])).mappings().one())
            task["goal"] = db.execute(select(events.c.text).where(events.c.id == key(task["id"], "initial"))).scalar() or task["title"]
            # No future messages and no private chat/profile/episodic memory.
            history = db.execute(select(events).where(events.c.task_id == task["id"], events.c.seq < event["seq"])
                                 .order_by(events.c.seq.desc()).limit(30)).mappings().all()
        return task, event, [dict(e) for e in reversed(history)]

    def finish(self, claim, result):
        with self.engine.begin() as db:
            task = self._require(db, claim["task_id"], lock=True)
            if task["status"] != "open" or not db.execute(select(runs.c.id).where(self._owned(claim))).first():
                raise LeaseLostError("事项运行租约已失效")
            event = db.execute(select(events).where(events.c.id == claim["id"])).mappings().one()
            data = {"run_id": claim["id"], "token_usage": result.get("token_usage", {}),
                    "conversation_state": result.get("conversation_state", {})}
            data.update(self.effects.commit(db, task, event, claim, result))
            if "turn_record" in result:
                from core.turn_records import record_turn
                record = dict(result["turn_record"])
                record["approval_id"] = data.get("approval_id")
                record_turn(db, record)
            if self.governance:
                self.governance.complete(db, claim, result)
            self._append(db, task, key("reply", claim["id"]), "assistant", "agent:it-copilot", "IT-Copilot",
                         result["response"], data)
            if not db.execute(update(runs).where(self._owned(claim)).values(status="done", lease_until=None, error=None)).rowcount:
                raise LeaseLostError("事项运行提交时租约已失效")

    def fail(self, claim, error):
        with self.engine.begin() as db:
            task = self._require(db, claim["task_id"], lock=True)
            failed = claim["attempts"] >= 3
            changed = db.execute(update(runs).where(self._owned(claim)).values(
                status="failed" if failed else "queued", lease_until=None, error=error,
                not_before=self.clock() + min(30, 2 ** claim["attempts"]))).rowcount
            if changed and self.governance:
                self.governance.complete(db, claim, error=error)
            if changed and failed:
                self._append(db, task, key("failure", claim["id"], claim["version"]), "system", "system:tasks", "任务服务",
                             "Agent 回合处理失败，可在此重试；已提交的审批仍可查看。", {"run_id": claim["id"]})
