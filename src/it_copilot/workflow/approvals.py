from __future__ import annotations

import hashlib
from contextlib import nullcontext
from typing import Any, Dict, List, Optional

from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from it_copilot.infra import db as schema
from it_copilot.infra.db import (
    actions,
    approval_decisions,
    approvals,
)
from it_copilot.infra.identity import Principal
from it_copilot.workflow.approval_queries import ApprovalQueries
from it_copilot.workflow.policy import PolicyViolation
from it_copilot.workflow.repository import (
    ApprovalConflict,
    ApprovalForbidden,
    ApprovalStateConflict,
    _action_key,
    _Retry,
    canonical,
)
from it_copilot.workflow.states import APPROVAL, EXECUTION, OPEN, ApprovalStatus, Execution


class ApprovalRepo(ApprovalQueries):
    def expire_pending(self, limit: int = 200) -> int:
        """Time is an event: expire due approvals even if no approver opens them.

        CAS and the notice share one transaction. A concurrent decision or another
        sweeper can win, but cannot generate a second terminal notice.
        """
        now = self.store.clock()
        with schema.read(self.store.engine) as db:
            ids = db.execute(select(approvals.c.id).where(
                approvals.c.status.in_(OPEN), approvals.c.expires_at <= now
            ).order_by(approvals.c.expires_at).limit(limit)).scalars().all()
        expired = 0
        for approval_id in ids:
            try:
                with self.store.transaction() as db:
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
        self.store._audit(db, row["id"], "expired", "system", detail={"draft": True} if draft else None)
        if not draft:
            self.store._notify(db, row["id"], "expired")

    def create(self, approval_id: str, *, request_id: str, conv_id: str, principal: Principal,
               action_requests: List[Dict[str, Any]], summary: Optional[Dict[str, Any]] = None, connection=None,
               ask_requester: bool = False, provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Freeze and store a request.

        ``ask_requester``: the caller can show the request back to its requester (chat, IM, shared
        task), so actions the policy lists under ``requester_confirmation`` start as a ``draft``
        that only enters the approver queue once the requester confirms it (``confirm``).
        ``provenance``: why the Agent proposed it (``it_copilot.workflow.approval.provenance``); written to the
        immutable ``created`` audit entry, outside the hash."""
        if not action_requests:
            raise ValueError("审批必须包含冻结的工具名称和参数")
        policy = self.store.policy
        frozen_actions = [policy.normalise(a) for a in action_requests]
        for action in frozen_actions:
            policy.check_requester(action, principal.employee_id, principal.roles)
        required = max(policy.requirement(a).approvals for a in frozen_actions)
        draft = ask_requester and policy.needs_confirmation(frozen_actions)
        frozen = {"request_id": request_id, "requester": principal.subject, "tenant": principal.tenant, "conv_id": conv_id,
                  "action_requests": frozen_actions, "policy_version": policy.version}
        digest = hashlib.sha256(canonical(frozen).encode()).hexdigest()
        now = self.store.clock()
        # Roles at filing time, outside the frozen hash: server-derived, and only used to
        # re-check on-behalf requests when the policy changes (ADR 0008).
        payload = {**(summary or {}), "action_requests": frozen_actions, "requester_roles": sorted(principal.roles)}
        superseded: List[str] = []
        carried: List[str] = []
        try:
            with (nullcontext(connection) if connection is not None else self.store.transaction()) as db:
                existing = db.execute(select(approvals.c.payload_hash).where(approvals.c.id == approval_id)).scalar()
                if existing is None:
                    db.execute(insert(approvals).values(
                        id=approval_id, request_id=request_id, conv_id=conv_id, requester=principal.subject, requester_name=principal.username,
                        requester_employee_id=principal.employee_id, department=principal.department, tenant=principal.tenant,
                        status=ApprovalStatus.DRAFT if draft else ApprovalStatus.PENDING, execution=Execution.NOT_EXECUTED,
                        payload_hash=digest, policy_version=policy.version,
                        approvals_required=required, payload=payload, version=0, created_at=now,
                        expires_at=now + (policy.confirmation_ttl_hours if draft else policy.approval_ttl_hours) * 3600,
                        confirmed_at=None if draft else now,
                    ))
                    detail = {"policy_version": policy.version, "approvals_required": required, "awaiting_confirmation": draft}
                    if provenance:
                        detail["provenance"] = provenance
                    self.store._audit(db, approval_id, "created", principal.subject, detail=detail)
                    # A new draft only replaces older drafts: a pending request stays until the
                    # requester confirms its replacement, so walking away from a draft loses nothing.
                    superseded = self._supersede(db, approval_id, conv_id, principal.subject, principal.tenant, frozen_actions, now,
                                                 statuses=(ApprovalStatus.DRAFT,) if draft else tuple(OPEN), carried=carried)
                row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().one()
                decisions = db.execute(select(approval_decisions).where(approval_decisions.c.approval_id == approval_id)).mappings().all()
                rows = db.execute(select(actions).where(actions.c.approval_id == approval_id).order_by(actions.c.seq)).mappings().all()
                body = self._body(row, decisions, rows, self._open_resources(db, row["tenant"]))
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
            self.store._audit(db, row["id"], "superseded", requester, detail=detail)
            replaced.append(row["id"])
        if replaced:
            self.store._audit(db, approval_id, "supersedes", requester, detail={"approvals": replaced})
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
            approvals_required=max(self.store.policy.requirement(a).approvals for a in rest), payload=payload, version=0,
            created_at=now, expires_at=row["expires_at"], confirmed_at=row["confirmed_at"],
        ))
        self.store._audit(db, rest_id, "created", row["requester"], detail={"carried_from": row["id"], "by": by,
                                                                        "policy_version": row["policy_version"]})
        return rest_id

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
        now = self.store.clock()
        with self.store.transaction() as db:
            row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
            if not row:
                return False
            if not self.store.policy.approver_roles.intersection(principal.roles):
                raise ApprovalForbidden("当前身份没有审批权限")
            if not self._shared_scope(row, principal):
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
            if row["policy_version"] != self.store.policy.version:
                # Frozen under an older policy: re-check before any vote counts.
                row = self.store.grants._revalidate_pending(db, row, now)
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
            db.execute(insert(approval_decisions).values(tenant=row["tenant"], approval_id=approval_id, actor=principal.subject, actor_name=principal.username, decision=decision, at=now))
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
            self.store._audit(db, approval_id, decision, principal.subject, detail={"approved_votes": approved, "required": row["approvals_required"]})
            if values.get("status") == ApprovalStatus.APPROVED:
                self.store.actions.transactions._enqueue(db, row, now)
                self.store._audit(db, approval_id, "execution_queued", "system")
            elif values.get("status") == ApprovalStatus.REJECTED:
                self.store._notify(db, approval_id, "rejected", {"by": principal.username})
        return True

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
        now = self.store.clock()
        with self.store.transaction() as db:
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
            self.store._audit(db, approval_id, "withdrawn", principal.subject, detail=detail or None)
            if not draft:  # nobody but the requester ever saw a draft: no notice to send
                self.store._notify(db, approval_id, "withdrawn", {"reason": reason})
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
        now = self.store.clock()
        with self.store.transaction() as db:
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
            if row["policy_version"] != self.store.policy.version:
                row = self.store.grants._revalidate_pending(db, row, now)
                if row is None:
                    return True
            self._cas(db, row, status=ApprovalStatus.PENDING, confirmed_at=now,
                      expires_at=now + self.store.policy.approval_ttl_hours * 3600)
            self.store._audit(db, approval_id, "confirmed", principal.subject, detail={"via": "card" if payload_hash else "message"})
            self._supersede(db, approval_id, row["conv_id"], row["requester"], row["tenant"], row["payload"]["action_requests"], now)
            self.store._notify(db, approval_id, "confirmed")
        return True

    def _may_have_filed(self, employee_id: str, approval) -> bool:
        """Whether the current policy still lets the original requester file for this employee."""
        requester = self._requester(approval)
        try:
            self.store.policy.check_requester({"arguments": {"employee_id": employee_id}}, *requester)
        except PolicyViolation:
            return False
        return True

    @staticmethod
    def _requester(row) -> tuple:
        """Required server-derived identity snapshot; malformed approvals cannot bypass policy."""
        return row["requester_employee_id"], frozenset(row["payload"]["requester_roles"])

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
