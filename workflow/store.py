"""Durable approval workflow: frozen requests, quorum decisions, outbox actions, grant ledger.

The lifecycles (approval, execution, action, grant) are declared in ``workflow.states``;
every status write here is checked against them.

Guarantees (and their limits)
-----------------------------
- Approval decisions are compare-and-swap on ``approvals.version``; concurrent or repeated
  decisions produce exactly one state transition.
- The decision that approves an approval inserts its action rows in the same transaction,
  so an approval can never be "approved but forgotten".
- Effects on external systems are at-least-once attempts made safe by idempotent adapters
  plus observe-before-retry reconciliation. This is not cross-system exactly-once.
"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from sqlalchemy import and_, false, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from core import db as schema
from core.db import (
    access_grants,
    actions,
    approval_decisions,
    approvals,
    audit,
    directory_devices,
    directory_employees,
    directory_grants,
    directory_tickets,
)
from core.identity import Principal
from workflow.actions import WRITE_ACTIONS
from workflow.policy import AccessPolicy, PolicyViolation, load_policy
from workflow.lifecycle import approval_phase, work_phase, WorkPhase
from workflow.states import ACTION, APPROVAL, EXECUTION, GRANT, NOTIFIED, OPEN, SETTLED, ActionStatus, ApprovalStatus, Execution, GrantStatus


# Sandbox directory. Usernames double as Gitea accounts in the compose deployment.
SEED_EMPLOYEES = (
    ("1001", "alice", "engineering"), ("1002", "dave", "engineering"), ("2001", "bob", "engineering"),
    ("2002", "carol", "engineering"), ("3001", "fiona", "finance"), ("9001", "erin", "it"), ("9002", "grace", "security"),
    ("E1001", "e1001", "engineering"), ("EMP1001", "emp1001", "engineering"),
)


class ApprovalForbidden(PermissionError):
    """Caller lacks authority or would violate separation of duties."""


class ApprovalConflict(ValueError):
    """Frozen content mismatch (hash or reused approval id)."""


class ApprovalStateConflict(ValueError):
    """The approval or grant is no longer in a state that allows the requested change."""


class LeaseLost(RuntimeError):
    """Another worker reclaimed the action after this worker's lease expired."""


class _Retry(Exception):
    pass


def _reversible(tool_name: str) -> bool:
    action = WRITE_ACTIONS.get(tool_name)
    return bool(action and action.reversible)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _action_key(action: Dict[str, Any]) -> tuple:
    """What a request is about: same tool, same target employee and, for access, same system."""
    args = action["arguments"]
    return (action["tool_name"], args.get("employee_id"), args.get("system") if action["tool_name"] == "request_access" else None)


_ACTION_NAMES = {"request_access": "申请权限", "reset_password": "重置密码", "unlock_account": "解锁账号", "create_ticket": "创建工单",
                 "lock_device": "远程锁定设备"}


def _describe(action_requests: List[Dict[str, Any]]) -> str:
    parts = []
    for action in action_requests:
        args = action["arguments"]
        what = _ACTION_NAMES.get(action["tool_name"], action["tool_name"])
        if action["tool_name"] == "request_access":
            what += f" {args.get('system')} {args.get('role')}"
        elif action["tool_name"] == "create_ticket":
            what += f"「{str(args.get('title', ''))[:40]}」"
        elif action["tool_name"] == "lock_device":
            what += f" {args.get('device_id')}"
        parts.append(f"{what}（工号 {args.get('employee_id')}）")
    return "；".join(parts)


def action_id(approval_id: str, payload_hash: str, seq: int, kind: str = "apply") -> str:
    return hashlib.sha256(f"{approval_id}:{payload_hash}:{seq}:{kind}".encode()).hexdigest()


class WorkflowStore:
    def __init__(self, url: str, *, policy: Optional[AccessPolicy] = None, clock: Callable[[], float] = time.time,
                 route: Callable[[Dict[str, Any]], str] = lambda action: "local", run_migrations: bool = True,
                 notify: Optional[Callable[[Any, Dict[str, Any]], None]] = None):
        self.url = url
        self.engine = schema.shared_engine(url) if run_migrations else schema.make_engine(url)
        self._policy = policy
        self.clock = clock
        self.route = route
        # Called inside the transaction that settles an approval (rejected, expired, invalidated,
        # execution finished), so a channel can queue its notice atomically with the state change.
        self.notify = notify
        self.shared_access = None
        self.shared_filter = None
        self._seed()

    @property
    def policy(self) -> AccessPolicy:
        """The injected policy, or the policy file as it is now (edits apply without restart)."""
        return self._policy or load_policy()

    @policy.setter
    def policy(self, value: AccessPolicy) -> None:
        self._policy = value

    def close(self) -> None:
        self.engine.dispose()

    def _seed(self) -> None:
        with self.engine.begin() as db:
            known = set(db.execute(select(directory_employees.c.id)).scalars())
            rows = [{"id": i, "username": u, "department": d} for i, u, d in SEED_EMPLOYEES if i not in known]
            if rows:
                db.execute(insert(directory_employees), rows)
            # One laptop per seeded employee: DEV-<employee id>.
            devices = set(db.execute(select(directory_devices.c.id)).scalars())
            rows = [{"id": f"DEV-{i}", "employee_id": i, "kind": "laptop", "locked": 0} for i, _, _ in SEED_EMPLOYEES if f"DEV-{i}" not in devices]
            if rows:
                db.execute(insert(directory_devices), rows)

    def username_of(self, employee_id: str) -> Optional[str]:
        with schema.read(self.engine) as db:
            return db.execute(select(directory_employees.c.username).where(directory_employees.c.id == employee_id)).scalar()

    def _audit(self, db, approval_id, event, actor, *, action_id=None, detail=None) -> None:
        db.execute(insert(audit).values(approval_id=approval_id, action_id=action_id, event=event, actor=actor[:200], detail=detail, at=self.clock()))

    def _notify(self, db, approval_id: str, event: str, detail: Optional[Dict[str, Any]] = None) -> None:
        if not self.notify:
            return
        row = db.execute(select(approvals.c.request_id, approvals.c.conv_id, approvals.c.requester)
                         .where(approvals.c.id == approval_id)).mappings().first()
        self.notify(db, {"approval_id": approval_id, "event": event, "detail": detail or {}, **row})

    # ── Approval lifecycle ──────────────────────────────────────────────────

    def expire_pending(self, limit: int = 200) -> int:
        """Time is an event: expire due approvals even if no approver opens them.

        CAS and the notice share one transaction. A concurrent decision or another
        sweeper can win, but cannot generate a second terminal notice.
        """
        now = self.clock()
        with schema.read(self.engine) as db:
            ids = db.execute(select(approvals.c.id).where(
                approvals.c.status.in_(OPEN), approvals.c.expires_at <= now
            ).order_by(approvals.c.expires_at).limit(limit)).scalars().all()
        expired = 0
        for approval_id in ids:
            try:
                with self.engine.begin() as db:
                    row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
                    if row is None or row["status"] not in OPEN or row["expires_at"] > now:
                        continue
                    self._expire(db, row, now)
                expired += 1
            except _Retry:
                continue
        return expired

    def _expire(self, db, row, now) -> None:
        """An unconfirmed draft just lapses; only a request approvers were waiting on gets a notice."""
        self._cas(db, row, status=ApprovalStatus.EXPIRED, decided_at=now)
        draft = row["status"] == ApprovalStatus.DRAFT
        self._audit(db, row["id"], "expired", "system", detail={"draft": True} if draft else None)
        if not draft:
            self._notify(db, row["id"], "expired")

    def create(self, approval_id: str, *, request_id: str, conv_id: str, principal: Principal,
               action_requests: List[Dict[str, Any]], summary: Optional[Dict[str, Any]] = None, connection=None,
               ask_requester: bool = False, provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Freeze and store a request.

        ``ask_requester``: the caller can show the request back to its requester (chat, IM, shared
        task), so actions the policy lists under ``requester_confirmation`` start as a ``draft``
        that only enters the approver queue once the requester confirms it (``confirm``).
        ``provenance``: why the Agent proposed it (``workflow.approval.provenance``); written to the
        immutable ``created`` audit entry, outside the hash."""
        if not action_requests:
            raise ValueError("审批必须包含冻结的工具名称和参数")
        frozen_actions = [self.policy.normalise(a) for a in action_requests]
        for action in frozen_actions:
            self.policy.check_requester(action, principal.employee_id, principal.roles)
        required = max(self.policy.requirement(a).approvals for a in frozen_actions)
        draft = ask_requester and self.policy.needs_confirmation(frozen_actions)
        frozen = {"request_id": request_id, "requester": principal.subject, "tenant": principal.tenant, "conv_id": conv_id,
                  "action_requests": frozen_actions, "policy_version": self.policy.version}
        digest = hashlib.sha256(canonical(frozen).encode()).hexdigest()
        now = self.clock()
        # Roles at filing time, outside the frozen hash: server-derived, and only used to
        # re-check on-behalf requests when the policy changes (ADR 0008).
        payload = {**(summary or {}), "action_requests": frozen_actions, "requester_roles": sorted(principal.roles)}
        superseded: List[str] = []
        carried: List[str] = []
        try:
            with (nullcontext(connection) if connection is not None else self.engine.begin()) as db:
                existing = db.execute(select(approvals.c.payload_hash).where(approvals.c.id == approval_id)).scalar()
                if existing is None:
                    db.execute(insert(approvals).values(
                        id=approval_id, request_id=request_id, conv_id=conv_id, requester=principal.subject, requester_name=principal.username,
                        requester_employee_id=principal.employee_id, department=principal.department, tenant=principal.tenant,
                        status=ApprovalStatus.DRAFT if draft else ApprovalStatus.PENDING, execution=Execution.NOT_EXECUTED,
                        payload_hash=digest, policy_version=self.policy.version,
                        approvals_required=required, payload=payload, version=0, created_at=now,
                        expires_at=now + (self.policy.confirmation_ttl_hours if draft else self.policy.approval_ttl_hours) * 3600,
                        confirmed_at=None if draft else now,
                    ))
                    detail = {"policy_version": self.policy.version, "approvals_required": required, "awaiting_confirmation": draft}
                    if provenance:
                        detail["provenance"] = provenance
                    self._audit(db, approval_id, "created", principal.subject, detail=detail)
                    # A new draft only replaces older drafts: a pending request stays until the
                    # requester confirms its replacement, so walking away from a draft loses nothing.
                    superseded = self._supersede(db, approval_id, conv_id, principal.subject, principal.tenant, frozen_actions, now,
                                                 statuses=(ApprovalStatus.DRAFT,) if draft else tuple(OPEN), carried=carried)
                row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().one()
                decisions = db.execute(select(approval_decisions).where(approval_decisions.c.approval_id == approval_id)).mappings().all()
                rows = db.execute(select(actions).where(actions.c.approval_id == approval_id).order_by(actions.c.seq)).mappings().all()
                body = self._body(row, decisions, rows)
        except IntegrityError:
            if connection is not None:
                raise
            body = self.get(approval_id)
            existing = body["payload_hash"]
        if existing is not None and existing != digest:
            raise ApprovalConflict("同一审批 ID 的参数不可修改；请创建新申请")
        return {**body, "superseded": superseded, "carried_over": carried}

    def _supersede(self, db, approval_id, conv_id, requester, tenant, frozen_actions, now, statuses=(ApprovalStatus.PENDING,),
                   carried: Optional[List[str]] = None) -> List[str]:
        """A requester who amends a request in the same conversation (adds details, says
        "confirm" again) replaces the still-pending one instead of queueing a duplicate.

        Every older request that shares an action with the new one is replaced. One turn can
        file several actions in a single request ({access, ticket}); when a later request
        amends only some of them, the rest is carried into a new request with the same frozen
        arguments, status and deadline (``_carry_over``), so nothing the requester still wants is
        dropped and nothing is filed twice. Requests about other things are left alone."""
        if not conv_id:
            return []
        keys = {_action_key(a) for a in frozen_actions}
        rows = db.execute(select(approvals).where(approvals.c.conv_id == conv_id, approvals.c.requester == requester, approvals.c.tenant == tenant,
                                                  approvals.c.status.in_(statuses), approvals.c.id != approval_id)
                          .order_by(approvals.c.created_at, approvals.c.id)).mappings().all()
        replaced = []
        for row in rows:
            old_actions = row["payload"]["action_requests"]
            rest = [a for a in old_actions if _action_key(a) not in keys]
            if len(rest) == len(old_actions):
                continue
            # Version-guarded: an approver deciding concurrently wins and the row is left as decided.
            done = db.execute(update(approvals).where(approvals.c.id == row["id"], approvals.c.version == row["version"],
                                                      approvals.c.status == row["status"])
                              .values(status=ApprovalStatus.SUPERSEDED, decided_at=now, version=row["version"] + 1)).rowcount
            if not done:
                continue
            detail = {"by": approval_id}
            if rest:
                detail["remainder"] = self._carry_over(db, row, rest, approval_id, now)
                if carried is not None:
                    carried.append(detail["remainder"])
            self._audit(db, row["id"], "superseded", requester, detail=detail)
            replaced.append(row["id"])
        if replaced:
            self._audit(db, approval_id, "supersedes", requester, detail={"approvals": replaced})
        return replaced

    def _carry_over(self, db, row, rest: List[Dict[str, Any]], by: str, now: float) -> str:
        """The part of a replaced request the new one did not amend, as its own request.

        Same requester, conversation, policy version, status and deadline as the original; the
        actions are the original frozen ones, so the requester has nothing new to confirm. Votes
        are not carried over: they were cast on the original hash."""
        rest_id = "carry-" + hashlib.sha256(f"{row['id']}:{by}".encode()).hexdigest()[:32]
        frozen = {"request_id": row["request_id"], "requester": row["requester"], "tenant": row["tenant"], "conv_id": row["conv_id"],
                  "action_requests": rest, "policy_version": row["policy_version"]}
        tools = [a["tool_name"] for a in rest]
        payload = {**row["payload"], "action_requests": rest, "carried_from": row["id"]}
        if "actions" in payload:
            payload["actions"] = tools
            payload["summary"] = f"请求执行 {', '.join(tools)}；当前仅生成 dry-run，需人工审批后执行。"
        db.execute(insert(approvals).values(
            id=rest_id, request_id=row["request_id"], conv_id=row["conv_id"], requester=row["requester"], requester_name=row["requester_name"],
            requester_employee_id=row["requester_employee_id"], department=row["department"], tenant=row["tenant"],
            status=row["status"], execution=Execution.NOT_EXECUTED,
            payload_hash=hashlib.sha256(canonical(frozen).encode()).hexdigest(), policy_version=row["policy_version"],
            approvals_required=max(self.policy.requirement(a).approvals for a in rest), payload=payload, version=0,
            created_at=now, expires_at=row["expires_at"], confirmed_at=row["confirmed_at"],
        ))
        self._audit(db, rest_id, "created", row["requester"], detail={"carried_from": row["id"], "by": by,
                                                                        "policy_version": row["policy_version"]})
        return rest_id

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with schema.read(self.engine) as db:
            row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
            if not row:
                return None
            decisions = db.execute(select(approval_decisions).where(approval_decisions.c.approval_id == approval_id).order_by(approval_decisions.c.id)).mappings().all()
            rows = db.execute(select(actions).where(actions.c.approval_id == approval_id).order_by(actions.c.seq)).mappings().all()
        return self._body(row, decisions, rows)

    def _body(self, row, decisions, rows) -> Dict[str, Any]:
        payload = dict(row["payload"])
        now = self.clock()

        def view(a):
            blocked = a["kind"] == "apply" and any(
                prior["kind"] == "apply" and prior["seq"] < a["seq"] and prior["status"] != ActionStatus.SUCCEEDED for prior in rows)
            return {"operation_id": a["id"], "seq": a["seq"], "kind": a["kind"], "action": a["tool_name"], "adapter": a["adapter"],
                    "status": a["status"], "phase": work_phase(a, now, blocked=blocked), "attempts": a["attempts"], "not_before": _iso(a["not_before"]), "error": a["error"],
                    **{k: v for k, v in (a["result"] or {}).items() if k != "baseline_level"}}

        return {
            **payload, "approval_id": row["id"], "request_id": row["request_id"], "conv_id": row["conv_id"],
            "user_id": row["requester"], "requester": row["requester"], "requester_employee_id": row["requester_employee_id"],
            "department": row["department"], "tenant": row["tenant"], "status": row["status"], "execution": row["execution"],
            "lifecycle": approval_phase(row, decisions, rows, now),
            "payload_hash": row["payload_hash"], "policy_version": row["policy_version"],
            "approvals_required": row["approvals_required"],
            "requester_name": row["requester_name"] or row["requester"],
            "decisions": [{"actor": d["actor"], "actor_name": d["actor_name"] or d["actor"], "decision": d["decision"], "at": _iso(d["at"])} for d in decisions],
            "created_at": _iso(row["created_at"]), "expires_at": _iso(row["expires_at"]), "decided_at": _iso(row["decided_at"]),
            "awaiting_confirmation": row["status"] == ApprovalStatus.DRAFT, "confirmed_at": _iso(row.get("confirmed_at")),
            "results": [view(a) for a in rows if a["kind"] == "apply"],
            "revocations": [view(a) for a in rows if a["kind"] == "revoke"],
        }

    def list(self, principal: Principal, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        private_scope = or_(approvals.c.requester == principal.subject, principal.can_read_all,
            bool(self.policy.approver_roles.intersection(principal.roles)))
        shared_scope = and_(approvals.c.tenant == principal.tenant,
            or_(approvals.c.requester == principal.subject, principal.is_admin))
        if self.shared_filter:
            shared_scope = self.shared_filter(principal)
        shared = approvals.c.conv_id.startswith('task:')
        query = select(approvals.c.id).where(approvals.c.tenant == principal.tenant,
            or_(and_(shared, shared_scope), and_(or_(approvals.c.conv_id.is_(None), ~shared), private_scope)))
        # A private draft is the requester's until confirmed: approvers do not see it in their queue.
        # Shared-task drafts stay visible to the room (members see the card waiting on the requester).
        query = query.where(or_(shared, approvals.c.status != ApprovalStatus.DRAFT, approvals.c.requester == principal.subject, principal.can_read_all))
        if status:
            query = query.where(approvals.c.status == status)
        query = query.order_by(approvals.c.created_at.desc()).limit(limit)
        with schema.read(self.engine) as db:
            ids = db.execute(query).scalars().all()
        return [self.get(i) for i in ids]

    def can_read(self, body: Dict[str, Any], principal: Principal) -> bool:
        if self.shared_access and (body.get('conv_id') or '').startswith('task:'):
            return self._shared_scope(body, principal)
        if body["status"] == ApprovalStatus.DRAFT:  # not yet submitted: the requester's own business
            return self._shared_scope(body, principal) and (body["requester"] == principal.subject or principal.can_read_all)
        return self._shared_scope(body, principal) and (body["requester"] == principal.subject or principal.can_read_all
                or bool(self.policy.approver_roles.intersection(principal.roles)))

    def _shared_scope(self, row, principal, db=None, decision=False):
        if row.get('tenant') != principal.tenant:
            return False
        if not (row.get("conv_id") or "").startswith("task:"):
            return True
        if self.shared_access:
            try:
                if db is not None:
                    return self.shared_access(db, row, principal, decision)
                with schema.read(self.engine) as connection:
                    return self.shared_access(connection, row, principal, decision)
            except ValueError:
                return False
        return principal.is_admin or row["requester"] == principal.subject

    def decide(self, approval_id: str, decision: str, principal: Principal, payload_hash: str) -> Optional[Dict[str, Any]]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision 必须为 approved 或 rejected")
        for _ in range(8):
            try:
                found = self._decide_once(approval_id, decision, principal, payload_hash)
            except (_Retry, IntegrityError):
                continue
            return self.get(approval_id) if found else None
        raise RuntimeError("审批并发冲突重试次数耗尽")

    def _decide_once(self, approval_id, decision, principal, payload_hash) -> bool:
        now = self.clock()
        with self.engine.begin() as db:
            row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
            if not row:
                return False
            if not self.policy.approver_roles.intersection(principal.roles):
                raise ApprovalForbidden("当前身份没有审批权限")
            if not self._shared_scope(row, principal, db, decision=True):
                raise ApprovalForbidden("无权处理该审批")
            if payload_hash != row["payload_hash"]:
                raise ApprovalConflict("审批内容哈希不匹配")
            if row["status"] == ApprovalStatus.DRAFT:
                raise ApprovalStateConflict("申请人尚未确认提交这条申请，暂不能审批")
            if row["status"] != ApprovalStatus.PENDING:
                return True  # idempotent: the decision already happened
            if now >= row["expires_at"]:
                self._expire(db, row, now)
                return True
            if row["policy_version"] != self.policy.version:
                # Frozen under an older policy: re-check before any vote counts.
                row = self._revalidate_pending(db, row, now)
                if row is None:
                    return True
            if principal.subject == row["requester"]:
                raise ApprovalForbidden("职责分离：不能审批自己发起的申请")
            targets = {a["arguments"]["employee_id"] for a in row["payload"]["action_requests"]}
            # Fail closed: without an employee id the approver cannot be shown not to be the target
            # (an on-behalf request filed by an admin for the approver's own account).
            if not principal.employee_id:
                raise ApprovalForbidden("职责分离：当前身份缺少工号，无法确认申请不涉及本人账号")
            if principal.employee_id in targets:
                raise ApprovalForbidden("职责分离：不能审批变更自己账号的申请")
            if db.execute(select(approval_decisions.c.id).where(approval_decisions.c.approval_id == approval_id,
                                                                 approval_decisions.c.actor == principal.subject)).first():
                return True  # same approver re-submitting
            db.execute(insert(approval_decisions).values(approval_id=approval_id, actor=principal.subject, actor_name=principal.username, decision=decision, at=now))
            approved = db.execute(select(func.count()).select_from(approval_decisions).where(
                approval_decisions.c.approval_id == approval_id, approval_decisions.c.decision == "approved")).scalar()
            values: Dict[str, Any] = {}
            if decision == "rejected":
                values = {"status": ApprovalStatus.REJECTED, "decided_at": now}
            elif approved >= row["approvals_required"]:
                values = {"status": ApprovalStatus.APPROVED, "execution": Execution.QUEUED, "decided_at": now}
            # Always bump the version: two approvers deciding concurrently must serialize,
            # otherwise each could count only its own vote and the quorum is never reached.
            self._cas(db, row, **values)
            self._audit(db, approval_id, decision, principal.subject, detail={"approved_votes": approved, "required": row["approvals_required"]})
            if values.get("status") == ApprovalStatus.APPROVED:
                self._enqueue(db, row, now)
                self._audit(db, approval_id, "execution_queued", "system")
            elif values.get("status") == ApprovalStatus.REJECTED:
                self._notify(db, approval_id, "rejected", {"by": principal.username})
        return True

    # ── Requester actions ───────────────────────────────────────────────────

    def withdraw(self, approval_id: str, principal: Principal, reason: str = "") -> Optional[Dict[str, Any]]:
        """The requester takes back a request nobody has settled yet.

        Shares the decision CAS: a withdrawal racing an approver's vote either lands first
        (the vote then sees a settled approval) or loses and reports the decided status.
        """
        for _ in range(8):
            try:
                found = self._withdraw_once(approval_id, principal, reason.strip()[:500])
            except _Retry:
                continue
            return self.get(approval_id) if found else None
        raise RuntimeError("审批并发冲突重试次数耗尽")

    def _withdraw_once(self, approval_id: str, principal: Principal, reason: str) -> bool:
        now = self.clock()
        with self.engine.begin() as db:
            row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
            if not row:
                return False
            if row["tenant"] != principal.tenant or row["requester"] != principal.subject:
                raise ApprovalForbidden("只有申请人本人可以撤回申请")
            if row["status"] == ApprovalStatus.WITHDRAWN:
                return True  # idempotent
            if row["status"] not in OPEN:
                raise ApprovalStateConflict(f"申请已是 {row['status']} 状态，不能撤回")
            if now >= row["expires_at"]:
                self._expire(db, row, now)
                return True
            draft = row["status"] == ApprovalStatus.DRAFT
            self._cas(db, row, status=ApprovalStatus.WITHDRAWN, decided_at=now)
            detail = {**({"reason": reason} if reason else {}), **({"draft": True} if draft else {})}
            self._audit(db, approval_id, "withdrawn", principal.subject, detail=detail or None)
            if not draft:  # nobody but the requester ever saw a draft: no notice to send
                self._notify(db, approval_id, "withdrawn", {"reason": reason})
        return True

    def confirm(self, approval_id: str, principal: Principal, payload_hash: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """The requester confirms a draft: it enters the approver queue with a fresh approval TTL.

        ``payload_hash`` binds the confirmation to what the requester was shown (the card); the
        typed "确认" path omits it and confirms the conversation's current draft, which the
        conversation lease keeps from changing underneath. Re-checks the policy like a decision
        does, then supersedes the older pending requests this one covers."""
        for _ in range(8):
            try:
                found = self._confirm_once(approval_id, principal, payload_hash)
            except _Retry:
                continue
            return self.get(approval_id) if found else None
        raise RuntimeError("审批并发冲突重试次数耗尽")

    def _confirm_once(self, approval_id: str, principal: Principal, payload_hash: Optional[str]) -> bool:
        now = self.clock()
        with self.engine.begin() as db:
            row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
            if not row:
                return False
            if row["tenant"] != principal.tenant or row["requester"] != principal.subject:
                raise ApprovalForbidden("只有申请人本人可以确认提交")
            if payload_hash is not None and payload_hash != row["payload_hash"]:
                raise ApprovalConflict("申请内容哈希不匹配")
            if row["status"] == ApprovalStatus.PENDING and row["confirmed_at"] is not None:
                return True  # idempotent
            if row["status"] != ApprovalStatus.DRAFT:
                raise ApprovalStateConflict(f"申请已是 {row['status']} 状态，不能确认提交")
            if now >= row["expires_at"]:
                self._expire(db, row, now)
                return True
            if row["policy_version"] != self.policy.version:
                row = self._revalidate_pending(db, row, now)
                if row is None:
                    return True
            self._cas(db, row, status=ApprovalStatus.PENDING, confirmed_at=now,
                      expires_at=now + self.policy.approval_ttl_hours * 3600)
            self._audit(db, approval_id, "confirmed", principal.subject, detail={"via": "card" if payload_hash else "message"})
            self._supersede(db, approval_id, row["conv_id"], row["requester"], row["tenant"], row["payload"]["action_requests"], now)
            self._notify(db, approval_id, "confirmed")
        return True

    def drafts(self, principal: Principal, conv_id: str) -> List[Dict[str, Any]]:
        """This requester's unconfirmed, unexpired drafts in one conversation, oldest first."""
        with schema.read(self.engine) as db:
            ids = db.execute(select(approvals.c.id).where(
                approvals.c.tenant == principal.tenant, approvals.c.requester == principal.subject, approvals.c.conv_id == conv_id,
                approvals.c.status == ApprovalStatus.DRAFT, approvals.c.expires_at > self.clock(),
            ).order_by(approvals.c.created_at, approvals.c.id)).scalars().all()
        return [{**body, "description": _describe(body["action_requests"])} for body in map(self.get, ids)]

    def requester_items(self, principal: Principal, limit: int = 5) -> List[Dict[str, Any]]:
        """What this person could act on right now: own drafts (confirm or cancel), own pending
        requests (withdraw) and live time-limited grants they requested or hold (release). Read-only; the same checks run
        again when the button is pressed."""
        now = self.clock()
        with schema.read(self.engine) as db:
            pending = db.execute(select(approvals).where(
                approvals.c.tenant == principal.tenant, approvals.c.requester == principal.subject,
                approvals.c.status.in_(OPEN), approvals.c.expires_at > now,
            ).order_by(approvals.c.created_at.desc()).limit(limit)).mappings().all()
            mine = or_(approvals.c.requester == principal.subject,
                       access_grants.c.employee_id == principal.employee_id if principal.employee_id else false())
            grants = db.execute(select(access_grants, approvals.c.created_at.label("filed_at"))
                                .join(approvals, approvals.c.id == access_grants.c.approval_id)
                                .join(actions, actions.c.id == access_grants.c.revoke_action_id)
                                .where(approvals.c.tenant == principal.tenant, mine, access_grants.c.status == GrantStatus.ACTIVE,
                                       actions.c.status == ActionStatus.QUEUED, actions.c.not_before > now)
                                .order_by(access_grants.c.expires_at).limit(limit)).mappings().all()
        items = [{"approval_id": row["id"], "kind": "confirm" if row["status"] == ApprovalStatus.DRAFT else "withdraw",
                  "summary": _describe(row["payload"]["action_requests"]),
                  "created_at": _iso(row["created_at"]), "expires_at": _iso(row["expires_at"])} for row in pending]
        items += [{"approval_id": g["approval_id"], "kind": "release", "summary": f"{g['system']} {g['role']} 权限（工号 {g['employee_id']}）",
                   "created_at": _iso(g["filed_at"]), "expires_at": _iso(g["expires_at"])} for g in grants]
        return items

    def release(self, approval_id: str, principal: Principal) -> Optional[Dict[str, Any]]:
        """Give back the time-limited access an approval granted before it expires.

        Nothing is revoked here: the revoke action queued when the grant took effect is
        simply made due now, so the worker applies the same drift check as at expiry and
        never removes access the employee had before or someone else changed since.
        Returns the approval with ``released`` listing the grants made due by this call.
        """
        now = self.clock()
        with self.engine.begin() as db:
            row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
            if not row:
                return None
            grants = db.execute(select(access_grants).where(access_grants.c.approval_id == approval_id)
                                .order_by(access_grants.c.action_id)).mappings().all()
            targets = {g["employee_id"] for g in grants}
            allowed = row["tenant"] == principal.tenant and (
                row["requester"] == principal.subject or principal.is_admin
                or bool(principal.employee_id and principal.employee_id in targets))
            if not allowed:
                raise ApprovalForbidden("只有申请人、权限持有人或 IT 管理员可以提前归还权限")
            active = [g for g in grants if g["status"] == GrantStatus.ACTIVE and g["revoke_action_id"]]
            if not active and not any(g["status"] == GrantStatus.REVOKED for g in grants):
                raise ApprovalStateConflict("该申请没有生效中的限时权限可归还")
            released: List[str] = []  # stays empty when everything was already given back: idempotent
            for grant in active:
                moved = db.execute(update(actions).where(actions.c.id == grant["revoke_action_id"], actions.c.status == ActionStatus.QUEUED,
                                                         actions.c.not_before > now)
                                   .values(not_before=now, version=actions.c.version + 1, updated_at=now)).rowcount
                if not moved:
                    continue  # already due, running or done
                db.execute(update(access_grants).where(access_grants.c.action_id == grant["action_id"])
                           .values(expires_at=now, updated_at=now))
                self._audit(db, approval_id, "grant_released", principal.subject, action_id=grant["action_id"],
                            detail={"system": grant["system"], "role": grant["role"], "scheduled_expiry": _iso(grant["expires_at"])})
                released.append(grant["action_id"])
        body = self.get(approval_id)
        return {**body, "released": released}

    # ── Policy changes ──────────────────────────────────────────────────────
    #
    # An approval freezes the policy version it was created under. When the policy file
    # changes, open work is re-checked against the new version instead of being trusted
    # or silently cancelled:
    #   pending approval   no longer allowed -> invalidated; needs more approvers -> quorum raised
    #   approved, queued   no longer allowed -> queued actions cancelled before any effect
    #   active grant       role withdrawn -> revoked now; max duration shortened -> expiry moved earlier
    # Loosening never widens frozen work: quorums only go up and expiries only move earlier.

    def revalidate(self) -> Dict[str, Any]:
        policy, now = self.policy, self.clock()
        report: Dict[str, Any] = {"policy_version": policy.version, "invalidated": [], "quorum_raised": [], "cancelled": [],
                                  "grants_revoked": [], "grants_shortened": []}
        with schema.read(self.engine) as db:
            pending = db.execute(select(approvals.c.id).where(approvals.c.status.in_(OPEN), approvals.c.policy_version != policy.version)).scalars().all()
            queued = db.execute(select(approvals.c.id).where(approvals.c.status == ApprovalStatus.APPROVED, approvals.c.policy_version != policy.version,
                                                             approvals.c.execution.in_((Execution.QUEUED, Execution.EXECUTING)))).scalars().all()
            grants = db.execute(select(access_grants.c.action_id).join(approvals, approvals.c.id == access_grants.c.approval_id)
                                .where(access_grants.c.status == GrantStatus.ACTIVE, approvals.c.policy_version != policy.version)).scalars().all()
        for approval_id in pending:
            try:
                with self.engine.begin() as db:
                    row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
                    if row["status"] not in OPEN:
                        continue
                    before = row["approvals_required"]
                    after = self._revalidate_pending(db, row, now)
            except _Retry:
                continue  # an approver decided meanwhile; decide() re-checks on its own
            if after is None:
                report["invalidated"].append(approval_id)
            elif after["approvals_required"] != before:
                report["quorum_raised"].append(approval_id)
        for approval_id in queued:
            with self.engine.begin() as db:
                if self._cancel_if_disallowed(db, approval_id, now):
                    report["cancelled"].append(approval_id)
        for grant_id in grants:
            with self.engine.begin() as db:
                outcome = self._recheck_grant(db, grant_id, now)
                if outcome:
                    report[outcome].append(grant_id)
        return report

    def _revalidate_pending(self, db, row, now):
        """Returns the refreshed row, or None when the approval was invalidated."""
        policy = self.policy
        frozen = row["payload"]["action_requests"]
        reason = policy.still_allows(frozen, self._requester(row))
        if reason:
            self._cas(db, row, status=ApprovalStatus.INVALIDATED, decided_at=now)
            self._audit(db, row["id"], "policy_invalidated", "system", detail={"from": row["policy_version"], "to": policy.version, "reason": reason})
            self._notify(db, row["id"], "invalidated", {"reason": reason})
            return None
        required = max(policy.requirement(a).approvals for a in frozen)
        if required > row["approvals_required"]:
            self._cas(db, row, approvals_required=required)
            self._audit(db, row["id"], "policy_quorum_raised", "system",
                        detail={"from": row["policy_version"], "to": policy.version, "required": [row["approvals_required"], required]})
        return db.execute(select(approvals).where(approvals.c.id == row["id"])).mappings().first()

    def _cancel_if_disallowed(self, db, approval_id: str, now: float) -> bool:
        row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
        reason = self.policy.still_allows(row["payload"]["action_requests"], self._requester(row))
        if not reason:
            return False
        # Only actions nobody started; one already running is stopped by the worker's own check.
        cancelled = db.execute(update(actions).where(actions.c.approval_id == approval_id, actions.c.kind == "apply",
                                                     actions.c.status == ActionStatus.QUEUED, actions.c.attempts == 0)
                               .values(status=ActionStatus.CANCELLED, error=reason[:500], version=actions.c.version + 1, updated_at=now)).rowcount
        if not cancelled:
            return False
        self._audit(db, approval_id, "policy_cancelled", "system", detail={"to": self.policy.version, "reason": reason, "actions": cancelled})
        self._refresh_execution(db, approval_id)
        return True

    def _recheck_grant(self, db, grant_id: str, now: float) -> Optional[str]:
        grant = db.execute(select(access_grants).where(access_grants.c.action_id == grant_id)).mappings().first()
        if grant["status"] != GrantStatus.ACTIVE or not grant["revoke_action_id"]:
            return None
        max_hours = self.policy.max_hours(grant["system"], grant["role"])
        approval = db.execute(select(approvals).where(approvals.c.id == grant["approval_id"])).mappings().first()
        if max_hours is None or not self._may_have_filed(grant["employee_id"], approval):
            expires_at, outcome = now, "grants_revoked"
        else:
            # The revoke row is written once, when the grant took effect; expires_at may already have been moved.
            granted_at = db.execute(select(actions.c.created_at).where(actions.c.id == grant["revoke_action_id"])).scalar()
            expires_at, outcome = granted_at + max_hours * 3600, "grants_shortened"
        if expires_at >= grant["expires_at"]:
            return None
        expires_at = max(expires_at, now)
        moved = db.execute(update(actions).where(actions.c.id == grant["revoke_action_id"], actions.c.status == ActionStatus.QUEUED)
                           .values(not_before=expires_at, version=actions.c.version + 1, updated_at=now)).rowcount
        if not moved:
            return None  # revocation already running or done
        db.execute(update(access_grants).where(access_grants.c.action_id == grant_id).values(expires_at=expires_at, updated_at=now))
        self._audit(db, grant["approval_id"], "policy_" + outcome.removeprefix("grants_"), "system", action_id=grant_id,
                    detail={"to": self.policy.version, "system": grant["system"], "role": grant["role"], "expires_at": _iso(expires_at)})
        return outcome

    def policy_block(self, approval_id: str) -> Optional[str]:
        """Checked by the worker right before an approval's first effect."""
        with schema.read(self.engine) as db:
            row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
        policy = self.policy
        if not row or row["policy_version"] == policy.version:
            return None
        return policy.still_allows(row["payload"]["action_requests"], self._requester(row))

    def _may_have_filed(self, employee_id: str, approval) -> bool:
        """Whether the current policy still lets the original requester file for this employee."""
        requester = self._requester(approval) if approval else None
        if requester is None:
            return True
        try:
            self.policy.check_requester({"arguments": {"employee_id": employee_id}}, *requester)
        except PolicyViolation:
            return False
        return True

    @staticmethod
    def _requester(row) -> Optional[tuple]:
        """(employee_id, roles) snapshotted at filing; None for approvals created before the snapshot."""
        roles = (row["payload"] or {}).get("requester_roles")
        return None if roles is None else (row["requester_employee_id"], frozenset(roles))

    @staticmethod
    def _cas(db, row, **values) -> None:
        if "status" in values:
            APPROVAL.check(row["status"], values["status"])
        if "execution" in values:
            EXECUTION.check(row["execution"], values["execution"])
        result = db.execute(update(approvals).where(approvals.c.id == row["id"], approvals.c.version == row["version"])
                            .values(version=row["version"] + 1, **values))
        if result.rowcount != 1:
            raise _Retry()

    def _enqueue(self, db, row, now) -> None:
        rows = []
        for seq, action in enumerate(row["payload"]["action_requests"]):
            rows.append({
                "id": action_id(row["id"], row["payload_hash"], seq), "approval_id": row["id"], "seq": seq, "kind": "apply",
                "tool_name": action["tool_name"], "arguments": action["arguments"], "adapter": self.route(action),
                "status": ActionStatus.QUEUED, "attempts": 0, "not_before": now, "version": 0, "created_at": now, "updated_at": now,
            })
        db.execute(insert(actions), rows)

    def requeue(self, approval_id: str, principal: Principal) -> Optional[Dict[str, Any]]:
        """Operator retry for manual_review/unknown actions: they re-enter as `unknown`,
        so the worker observes the target before deciding to apply again."""
        now = self.clock()
        with self.engine.begin() as db:
            approval = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
            if not approval:
                return None
            if not self._shared_scope(approval, principal):
                raise ApprovalForbidden("无权处理该审批")
            result = db.execute(update(actions).where(actions.c.approval_id == approval_id, actions.c.status.in_((ActionStatus.MANUAL_REVIEW, ActionStatus.UNKNOWN)))
                                .values(status=ActionStatus.UNKNOWN, attempts=0, not_before=now, lease_owner=None, lease_until=None,
                                        version=actions.c.version + 1, updated_at=now))
            self._audit(db, approval_id, "reconcile_requested", principal.subject, detail={"actions": result.rowcount})
            self._refresh_execution(db, approval_id)
        return self.get(approval_id)

    # ── Worker side ─────────────────────────────────────────────────────────

    def claim(self, worker_id: str, lease_s: float, limit: int = 10, *, exclude_ids=()) -> List[Dict[str, Any]]:
        now = self.clock()
        due = or_(and_(actions.c.status.in_((ActionStatus.QUEUED, ActionStatus.UNKNOWN)), actions.c.not_before <= now),
                  and_(actions.c.status == ActionStatus.EXECUTING, actions.c.lease_until < now))
        earlier = actions.alias("earlier_action")
        blocked = select(earlier.c.id).where(
            earlier.c.approval_id == actions.c.approval_id, earlier.c.kind == "apply",
            earlier.c.seq < actions.c.seq, earlier.c.status != ActionStatus.SUCCEEDED,
        ).exists()
        due = and_(due, or_(actions.c.kind != "apply", ~blocked))
        with schema.read(self.engine) as db:
            candidates = db.execute(select(actions).where(due, actions.c.id.not_in(exclude_ids)).order_by(actions.c.not_before, actions.c.seq).limit(limit * 4)).mappings().all()
        claimed = []
        for row in candidates:
            if work_phase(row, now) not in {WorkPhase.READY, WorkPhase.RECLAIMABLE}:
                continue
            if len(claimed) >= limit:
                break
            with self.engine.begin() as db:
                if row["kind"] == "apply" and db.execute(select(actions.c.id).where(
                        actions.c.approval_id == row["approval_id"], actions.c.kind == "apply",
                        actions.c.seq < row["seq"], actions.c.status != ActionStatus.SUCCEEDED)).first():
                    continue  # apply actions of one approval run strictly in order
                ACTION.check(row["status"], ActionStatus.EXECUTING)
                result = db.execute(update(actions).where(actions.c.id == row["id"], actions.c.version == row["version"]).values(
                    status=ActionStatus.EXECUTING, lease_owner=worker_id, lease_until=now + lease_s, attempts=row["attempts"] + 1,
                    version=row["version"] + 1, updated_at=now))
                if result.rowcount != 1:
                    continue  # another worker won the CAS
                if row["kind"] == "apply" and row["seq"] == 0 and row["attempts"] == 0:
                    db.execute(update(approvals).where(approvals.c.id == row["approval_id"], approvals.c.execution == Execution.QUEUED).values(execution=Execution.EXECUTING))
                self._audit(db, row["approval_id"], f"{row['kind']}_started", worker_id, action_id=row["id"], detail={"attempt": row["attempts"] + 1, "previous": row["status"]})
            claimed.append({**row, "prev_status": row["status"], "status": ActionStatus.EXECUTING, "attempts": row["attempts"] + 1,
                            "version": row["version"] + 1, "lease_owner": worker_id})
        return claimed

    def renew(self, action, lease_s):
        """Attempts is the claim epoch; intermediate result writes may change version."""
        now = self.clock()
        with self.engine.begin() as db:
            return db.execute(update(actions).where(
                actions.c.id == action["id"], actions.c.lease_owner == action["lease_owner"],
                actions.c.attempts == action["attempts"], actions.c.status == ActionStatus.EXECUTING,
                actions.c.lease_until >= now,
            ).values(lease_until=now + lease_s)).rowcount == 1

    def _action_cas(self, db, action, **values) -> None:
        if "status" in values:
            ACTION.check(action["status"], values["status"])
        result = db.execute(update(actions).where(actions.c.id == action["id"], actions.c.version == action["version"],
                                                  actions.c.lease_owner == action["lease_owner"], actions.c.lease_until >= self.clock())
                            .values(version=action["version"] + 1, updated_at=self.clock(), **values))
        if result.rowcount != 1:
            raise LeaseLost(action["id"])
        action["version"] += 1
        action["status"] = values.get("status", action["status"])

    def save_result(self, action: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Persist intermediate facts (e.g. the pre-change baseline) before touching the target,
        so a crash-and-retry does not mistake our own grant for the user's original access."""
        with self.engine.begin() as db:
            self._action_cas(db, action, result=result)
        action["result"] = result

    def apply_actions(self, approval_id: str) -> List[Dict[str, Any]]:
        with schema.read(self.engine) as db:
            return [dict(r) for r in db.execute(select(actions).where(actions.c.approval_id == approval_id, actions.c.kind == "apply").order_by(actions.c.seq)).mappings()]

    def succeed(self, action: Dict[str, Any], result: Dict[str, Any], *, preexisting: bool = False) -> None:
        now = self.clock()
        with self.engine.begin() as db:
            self._action_cas(db, action, status=ActionStatus.SUCCEEDED, result=result, error=None, lease_owner=None, lease_until=None)
            args = action["arguments"]
            if action["kind"] == "apply" and action["tool_name"] == "request_access":
                baseline = (action.get("result") or {}).get("baseline_level")
                expires_at = now + int(args["duration_hours"]) * 3600
                revoke_id = None
                if not preexisting:
                    revoke_id = hashlib.sha256(f"{action['id']}:revoke".encode()).hexdigest()
                    db.execute(insert(actions).values(
                        id=revoke_id, approval_id=action["approval_id"], seq=1000 + action["seq"], kind="revoke",
                        tool_name="revoke_access", arguments={"employee_id": args["employee_id"], "system": args["system"],
                        "role": args["role"], "baseline_level": baseline}, adapter=action["adapter"], status=ActionStatus.QUEUED, attempts=0,
                        not_before=expires_at, version=0, parent_action_id=action["id"], created_at=now, updated_at=now))
                db.execute(insert(access_grants).values(
                    action_id=action["id"], approval_id=action["approval_id"], employee_id=args["employee_id"], system=args["system"],
                    role=args["role"], adapter=action["adapter"], baseline_role=baseline, status=GrantStatus.PREEXISTING if preexisting else GrantStatus.ACTIVE,
                    expires_at=expires_at, revoke_action_id=revoke_id, updated_at=now))
            elif action["kind"] == "revoke":
                self._settle_grant(db, action["id"], GrantStatus.REVOKED, now)
            event = f"{action['kind']}_succeeded" + ("_preexisting" if preexisting else "")
            self._audit(db, action["approval_id"], event, action["lease_owner"], action_id=action["id"], detail=result)
            self._refresh_execution(db, action["approval_id"])

    def retry_later(self, action: Dict[str, Any], status: str, error: str, delay_s: float) -> None:
        """status=queued for transient failures, unknown when the outcome must be reconciled first."""
        with self.engine.begin() as db:
            self._action_cas(db, action, status=status, error=error[:500], not_before=self.clock() + delay_s, lease_owner=None, lease_until=None)
            self._audit(db, action["approval_id"], f"{action['kind']}_{status}", action["lease_owner"], action_id=action["id"], detail={"error": error[:500]})

    def fail(self, action: Dict[str, Any], error: str, *, status: str = ActionStatus.FAILED, ledger_status: Optional[str] = None) -> None:
        now = self.clock()
        with self.engine.begin() as db:
            self._action_cas(db, action, status=status, error=error[:500], lease_owner=None, lease_until=None)
            self._audit(db, action["approval_id"], f"{action['kind']}_{status}", action["lease_owner"], action_id=action["id"], detail={"error": error[:500]})
            if action["kind"] == "revoke":
                self._settle_grant(db, action["id"], ledger_status or GrantStatus.REVOKE_FAILED, now)
            else:
                self._compensate(db, action, now)
            self._refresh_execution(db, action["approval_id"])

    @staticmethod
    def _settle_grant(db, revoke_action_id: str, status: str, now: float) -> None:
        db.execute(update(access_grants).where(access_grants.c.revoke_action_id == revoke_action_id,
                                               access_grants.c.status.in_(GRANT.sources(status))).values(status=status, updated_at=now))

    def _compensate(self, db, failed: Dict[str, Any], now: float) -> None:
        """Saga step: cancel the rest of the batch and revoke grants it already made."""
        db.execute(update(actions).where(actions.c.approval_id == failed["approval_id"], actions.c.kind == "apply",
                                         actions.c.seq > failed["seq"], actions.c.status == ActionStatus.QUEUED)
                   .values(status=ActionStatus.CANCELLED, version=actions.c.version + 1, updated_at=now))
        done = db.execute(select(actions).where(actions.c.approval_id == failed["approval_id"], actions.c.kind == "apply",
                                                actions.c.seq < failed["seq"], actions.c.status == ActionStatus.SUCCEEDED)).mappings().all()
        for row in done:
            revoke = db.execute(update(actions).where(actions.c.parent_action_id == row["id"], actions.c.status == ActionStatus.QUEUED)
                                .values(not_before=now, version=actions.c.version + 1, updated_at=now))
            event = "compensation_scheduled" if revoke.rowcount else "compensation_unavailable"
            self._audit(db, failed["approval_id"], event, "system", action_id=row["id"], detail={"tool": row["tool_name"]})

    def _refresh_execution(self, db, approval_id: str) -> None:
        previous = db.execute(select(approvals.c.execution).where(approvals.c.id == approval_id)).scalar()
        rows = db.execute(select(actions.c.status, actions.c.tool_name).where(actions.c.approval_id == approval_id, actions.c.kind == "apply")).all()
        statuses = [r.status for r in rows]
        if not statuses:
            return
        if all(s == ActionStatus.SUCCEEDED for s in statuses):
            state = Execution.SUCCEEDED
        elif any(s not in SETTLED for s in statuses):
            state = Execution.QUEUED if all(s == ActionStatus.QUEUED for s in statuses) else Execution.EXECUTING
        elif ActionStatus.MANUAL_REVIEW in statuses or any(r.status == ActionStatus.SUCCEEDED and not _reversible(r.tool_name) for r in rows):
            # A failure after a non-reversible effect (password reset, ticket) needs a human.
            state = Execution.MANUAL_REVIEW
        else:
            state = Execution.FAILED
        EXECUTION.check(previous, state)
        db.execute(update(approvals).where(approvals.c.id == approval_id).values(execution=state))
        if state != previous and state in NOTIFIED:
            done = db.execute(select(actions.c.result, actions.c.error).where(actions.c.approval_id == approval_id, actions.c.kind == "apply")
                              .order_by(actions.c.seq)).all()
            self._notify(db, approval_id, "execution_" + state, {"error": next((a.error for a in done if a.error), None),
                                                                 "results": [a.result for a in done if a.result]})

    def pending_work(self, approval_id: Optional[str] = None) -> int:
        query = select(func.count()).select_from(actions).where(actions.c.status.in_((ActionStatus.QUEUED, ActionStatus.EXECUTING, ActionStatus.UNKNOWN)), actions.c.not_before <= self.clock())
        if approval_id:
            query = query.where(actions.c.approval_id == approval_id)
        with schema.read(self.engine) as db:
            return db.execute(query).scalar()

    # ── Read models ─────────────────────────────────────────────────────────

    def employee(self, employee_id: str) -> Optional[Dict[str, Any]]:
        with schema.read(self.engine) as db:
            row = db.execute(select(directory_employees).where(directory_employees.c.id == employee_id)).mappings().first()
            if not row:
                return None
            grants = db.execute(select(directory_grants).where(directory_grants.c.employee_id == employee_id).order_by(directory_grants.c.system)).mappings().all()
            tickets = db.execute(select(directory_tickets.c.id, directory_tickets.c.title).where(directory_tickets.c.employee_id == employee_id)).all()
            devices = db.execute(select(directory_devices).where(directory_devices.c.employee_id == employee_id).order_by(directory_devices.c.id)).mappings().all()
            ledger = db.execute(select(access_grants).where(access_grants.c.employee_id == employee_id).order_by(access_grants.c.updated_at)).mappings().all()
        return {
            "employee_id": row["id"], "username": row["username"], "department": row["department"], "active": row["active"],
            "locked": row["locked"], "password_version": row["password_version"],
            "grants": [{"system": g["system"], "role": g["role"], "operation_id": g["operation_id"]} for g in grants],
            "tickets": [{"ticket_id": t.id, "title": t.title} for t in tickets],
            "devices": [{"device_id": d["id"], "kind": d["kind"], "locked": bool(d["locked"])} for d in devices],
            "access_ledger": [{"action_id": g["action_id"], "approval_id": g["approval_id"], "system": g["system"], "role": g["role"],
                               "adapter": g["adapter"], "status": g["status"], "baseline_role": g["baseline_role"],
                               "expires_at": _iso(g["expires_at"])} for g in ledger],
        }

    def audit(self, approval_id: str) -> List[Dict[str, Any]]:
        with schema.read(self.engine) as db:
            rows = db.execute(select(audit).where(audit.c.approval_id == approval_id).order_by(audit.c.id)).mappings().all()
        return [{"event": r["event"], "actor": r["actor"], "action_id": r["action_id"], "detail": r["detail"], "at": _iso(r["at"])} for r in rows]

    def reset_directory(self, employee_ids: Iterable[str]) -> None:
        """Evaluation helper: restore seeded sandbox state between sequential cases."""
        from sqlalchemy import delete
        ids = list(employee_ids)
        with self.engine.begin() as db:
            db.execute(delete(directory_grants).where(directory_grants.c.employee_id.in_(ids)))
            db.execute(delete(directory_tickets).where(directory_tickets.c.employee_id.in_(ids)))
            db.execute(update(directory_employees).where(directory_employees.c.id.in_(ids)).values(locked=1, password_version=0, active=1))
            db.execute(update(directory_devices).where(directory_devices.c.employee_id.in_(ids)).values(locked=0, operation_id=None))
