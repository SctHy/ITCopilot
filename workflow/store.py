"""Durable approval workflow: frozen requests, quorum decisions, outbox actions, grant ledger.

State machines
--------------
approval.status     pending -> approved | rejected | expired | superseded
approval.execution  not_executed -> queued -> executing -> succeeded | failed | manual_review
action.status       queued -> executing -> succeeded
                                     \\-> unknown (ambiguous outcome; reconcile before retry)
                                     \\-> failed | manual_review | cancelled

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
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from core.identity import Principal
from workflow import db as schema
from workflow.db import access_grants, actions, approval_decisions, approvals, audit, directory_employees, directory_grants, directory_tickets
from workflow.policy import AccessPolicy, load_policy

TERMINAL = {"succeeded", "failed", "cancelled", "manual_review"}

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


class LeaseLost(RuntimeError):
    """Another worker reclaimed the action after this worker's lease expired."""


class _Retry(Exception):
    pass


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _action_key(action: Dict[str, Any]) -> tuple:
    """What a request is about: same tool, same target employee and, for access, same system."""
    args = action["arguments"]
    return (action["tool_name"], args.get("employee_id"), args.get("system") if action["tool_name"] == "request_access" else None)


def action_id(approval_id: str, payload_hash: str, seq: int, kind: str = "apply") -> str:
    return hashlib.sha256(f"{approval_id}:{payload_hash}:{seq}:{kind}".encode()).hexdigest()


class WorkflowStore:
    def __init__(self, url: str, *, policy: Optional[AccessPolicy] = None, clock: Callable[[], float] = time.time,
                 route: Callable[[Dict[str, Any]], str] = lambda action: "local", run_migrations: bool = True):
        if run_migrations:
            schema.migrate(url)
        self.url = url
        self.engine = schema.make_engine(url)
        self.policy = policy or load_policy()
        self.clock = clock
        self.route = route
        self._seed()

    def close(self) -> None:
        self.engine.dispose()

    def _seed(self) -> None:
        with self.engine.begin() as db:
            known = set(db.execute(select(directory_employees.c.id)).scalars())
            rows = [{"id": i, "username": u, "department": d} for i, u, d in SEED_EMPLOYEES if i not in known]
            if rows:
                db.execute(insert(directory_employees), rows)

    def username_of(self, employee_id: str) -> Optional[str]:
        with self.engine.connect() as db:
            return db.execute(select(directory_employees.c.username).where(directory_employees.c.id == employee_id)).scalar()

    def _audit(self, db, approval_id, event, actor, *, action_id=None, detail=None) -> None:
        db.execute(insert(audit).values(approval_id=approval_id, action_id=action_id, event=event, actor=actor[:200], detail=detail, at=self.clock()))

    # ── Approval lifecycle ──────────────────────────────────────────────────

    def create(self, approval_id: str, *, request_id: str, conv_id: str, principal: Principal,
               action_requests: List[Dict[str, Any]], summary: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not action_requests:
            raise ValueError("审批必须包含冻结的工具名称和参数")
        frozen_actions = [self.policy.normalise(a) for a in action_requests]
        for action in frozen_actions:
            self.policy.check_requester(action, principal.employee_id, principal.roles)
        required = max(self.policy.requirement(a).approvals for a in frozen_actions)
        frozen = {"request_id": request_id, "requester": principal.subject, "conv_id": conv_id,
                  "action_requests": frozen_actions, "policy_version": self.policy.version}
        digest = hashlib.sha256(canonical(frozen).encode()).hexdigest()
        now = self.clock()
        payload = {**(summary or {}), "action_requests": frozen_actions}
        superseded: List[str] = []
        try:
            with self.engine.begin() as db:
                existing = db.execute(select(approvals.c.payload_hash).where(approvals.c.id == approval_id)).scalar()
                if existing is None:
                    db.execute(insert(approvals).values(
                        id=approval_id, request_id=request_id, conv_id=conv_id, requester=principal.subject, requester_name=principal.username,
                        requester_employee_id=principal.employee_id, department=principal.department, tenant=principal.tenant,
                        status="pending", execution="not_executed", payload_hash=digest, policy_version=self.policy.version,
                        approvals_required=required, payload=payload, version=0, created_at=now,
                        expires_at=now + self.policy.approval_ttl_hours * 3600,
                    ))
                    self._audit(db, approval_id, "created", principal.subject, detail={"policy_version": self.policy.version, "approvals_required": required})
                    superseded = self._supersede(db, approval_id, conv_id, principal.subject, frozen_actions, now)
        except IntegrityError:
            existing = self.get(approval_id)["payload_hash"]
        if existing is not None and existing != digest:
            raise ApprovalConflict("同一审批 ID 的参数不可修改；请创建新申请")
        return {**self.get(approval_id), "superseded": superseded}

    def _supersede(self, db, approval_id, conv_id, requester, frozen_actions, now) -> List[str]:
        """A requester who amends a request in the same conversation (adds details, says
        "confirm" again) replaces the still-pending one instead of queueing a duplicate.
        Only approvals whose every action is covered by the new one are replaced, so
        unrelated pending requests in the conversation are left alone."""
        if not conv_id:
            return []
        keys = {_action_key(a) for a in frozen_actions}
        rows = db.execute(select(approvals).where(approvals.c.conv_id == conv_id, approvals.c.requester == requester,
                                                  approvals.c.status == "pending", approvals.c.id != approval_id)).mappings().all()
        replaced = []
        for row in rows:
            if {_action_key(a) for a in row["payload"]["action_requests"]} <= keys:
                # Version-guarded: an approver deciding concurrently wins and the row is left as decided.
                done = db.execute(update(approvals).where(approvals.c.id == row["id"], approvals.c.version == row["version"],
                                                          approvals.c.status == "pending")
                                  .values(status="superseded", decided_at=now, version=row["version"] + 1)).rowcount
                if not done:
                    continue
                self._audit(db, row["id"], "superseded", requester, detail={"by": approval_id})
                replaced.append(row["id"])
        if replaced:
            self._audit(db, approval_id, "supersedes", requester, detail={"approvals": replaced})
        return replaced

    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with self.engine.connect() as db:
            row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
            if not row:
                return None
            decisions = db.execute(select(approval_decisions).where(approval_decisions.c.approval_id == approval_id).order_by(approval_decisions.c.id)).mappings().all()
            rows = db.execute(select(actions).where(actions.c.approval_id == approval_id).order_by(actions.c.seq)).mappings().all()
        return self._body(row, decisions, rows)

    def _body(self, row, decisions, rows) -> Dict[str, Any]:
        payload = dict(row["payload"])

        def view(a):
            return {"operation_id": a["id"], "seq": a["seq"], "kind": a["kind"], "action": a["tool_name"], "adapter": a["adapter"],
                    "status": a["status"], "attempts": a["attempts"], "not_before": _iso(a["not_before"]), "error": a["error"],
                    **{k: v for k, v in (a["result"] or {}).items() if k != "baseline_level"}}

        return {
            **payload, "approval_id": row["id"], "request_id": row["request_id"], "conv_id": row["conv_id"],
            "user_id": row["requester"], "requester": row["requester"], "requester_employee_id": row["requester_employee_id"],
            "department": row["department"], "status": row["status"], "execution": row["execution"],
            "payload_hash": row["payload_hash"], "policy_version": row["policy_version"],
            "approvals_required": row["approvals_required"],
            "requester_name": row["requester_name"] or row["requester"],
            "decisions": [{"actor": d["actor"], "actor_name": d["actor_name"] or d["actor"], "decision": d["decision"], "at": _iso(d["at"])} for d in decisions],
            "created_at": _iso(row["created_at"]), "expires_at": _iso(row["expires_at"]), "decided_at": _iso(row["decided_at"]),
            "results": [view(a) for a in rows if a["kind"] == "apply"],
            "revocations": [view(a) for a in rows if a["kind"] == "revoke"],
        }

    def list(self, principal: Principal, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        query = select(approvals.c.id).order_by(approvals.c.created_at.desc()).limit(limit)
        if status:
            query = query.where(approvals.c.status == status)
        if not (principal.can_read_all or self.policy.approver_roles.intersection(principal.roles)):
            query = query.where(approvals.c.requester == principal.subject)
        with self.engine.connect() as db:
            ids = db.execute(query).scalars().all()
        return [self.get(i) for i in ids]

    def can_read(self, body: Dict[str, Any], principal: Principal) -> bool:
        return (body["requester"] == principal.subject or principal.can_read_all
                or bool(self.policy.approver_roles.intersection(principal.roles)))

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
            if payload_hash != row["payload_hash"]:
                raise ApprovalConflict("审批内容哈希不匹配")
            if row["status"] != "pending":
                return True  # idempotent: the decision already happened
            if now > row["expires_at"]:
                self._cas(db, row, status="expired", decided_at=now)
                self._audit(db, approval_id, "expired", "system")
                return True
            if not self.policy.approver_roles.intersection(principal.roles):
                raise ApprovalForbidden("当前身份没有审批权限")
            if principal.subject == row["requester"]:
                raise ApprovalForbidden("职责分离：不能审批自己发起的申请")
            targets = {a["arguments"]["employee_id"] for a in row["payload"]["action_requests"]}
            if principal.employee_id and principal.employee_id in targets:
                raise ApprovalForbidden("职责分离：不能审批变更自己账号的申请")
            if db.execute(select(approval_decisions.c.id).where(approval_decisions.c.approval_id == approval_id,
                                                                 approval_decisions.c.actor == principal.subject)).first():
                return True  # same approver re-submitting
            db.execute(insert(approval_decisions).values(approval_id=approval_id, actor=principal.subject, actor_name=principal.username, decision=decision, at=now))
            approved = db.execute(select(func.count()).select_from(approval_decisions).where(
                approval_decisions.c.approval_id == approval_id, approval_decisions.c.decision == "approved")).scalar()
            values: Dict[str, Any] = {}
            if decision == "rejected":
                values = {"status": "rejected", "decided_at": now}
            elif approved >= row["approvals_required"]:
                values = {"status": "approved", "execution": "queued", "decided_at": now}
            # Always bump the version: two approvers deciding concurrently must serialize,
            # otherwise each could count only its own vote and the quorum is never reached.
            self._cas(db, row, **values)
            self._audit(db, approval_id, decision, principal.subject, detail={"approved_votes": approved, "required": row["approvals_required"]})
            if values.get("status") == "approved":
                self._enqueue(db, row, now)
                self._audit(db, approval_id, "execution_queued", "system")
        return True

    @staticmethod
    def _cas(db, row, **values) -> None:
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
                "status": "queued", "attempts": 0, "not_before": now, "version": 0, "created_at": now, "updated_at": now,
            })
        db.execute(insert(actions), rows)

    def requeue(self, approval_id: str, principal: Principal) -> Optional[Dict[str, Any]]:
        """Operator retry for manual_review/unknown actions: they re-enter as `unknown`,
        so the worker observes the target before deciding to apply again."""
        now = self.clock()
        with self.engine.begin() as db:
            if not db.execute(select(approvals.c.id).where(approvals.c.id == approval_id)).first():
                return None
            result = db.execute(update(actions).where(actions.c.approval_id == approval_id, actions.c.status.in_(("manual_review", "unknown")))
                                .values(status="unknown", attempts=0, not_before=now, lease_owner=None, lease_until=None,
                                        version=actions.c.version + 1, updated_at=now))
            self._audit(db, approval_id, "reconcile_requested", principal.subject, detail={"actions": result.rowcount})
            self._refresh_execution(db, approval_id)
        return self.get(approval_id)

    # ── Worker side ─────────────────────────────────────────────────────────

    def claim(self, worker_id: str, lease_s: float, limit: int = 10) -> List[Dict[str, Any]]:
        now = self.clock()
        due = or_(and_(actions.c.status.in_(("queued", "unknown")), actions.c.not_before <= now),
                  and_(actions.c.status == "executing", actions.c.lease_until < now))
        with self.engine.connect() as db:
            candidates = db.execute(select(actions).where(due).order_by(actions.c.not_before, actions.c.seq).limit(limit * 4)).mappings().all()
        claimed = []
        for row in candidates:
            if len(claimed) >= limit:
                break
            with self.engine.begin() as db:
                if row["kind"] == "apply" and db.execute(select(actions.c.id).where(
                        actions.c.approval_id == row["approval_id"], actions.c.kind == "apply",
                        actions.c.seq < row["seq"], actions.c.status != "succeeded")).first():
                    continue  # apply actions of one approval run strictly in order
                result = db.execute(update(actions).where(actions.c.id == row["id"], actions.c.version == row["version"]).values(
                    status="executing", lease_owner=worker_id, lease_until=now + lease_s, attempts=row["attempts"] + 1,
                    version=row["version"] + 1, updated_at=now))
                if result.rowcount != 1:
                    continue  # another worker won the CAS
                if row["kind"] == "apply" and row["seq"] == 0 and row["attempts"] == 0:
                    db.execute(update(approvals).where(approvals.c.id == row["approval_id"], approvals.c.execution == "queued").values(execution="executing"))
                self._audit(db, row["approval_id"], f"{row['kind']}_started", worker_id, action_id=row["id"], detail={"attempt": row["attempts"] + 1, "previous": row["status"]})
            claimed.append({**row, "prev_status": row["status"], "status": "executing", "attempts": row["attempts"] + 1,
                            "version": row["version"] + 1, "lease_owner": worker_id})
        return claimed

    def _action_cas(self, db, action, **values) -> None:
        result = db.execute(update(actions).where(actions.c.id == action["id"], actions.c.version == action["version"],
                                                  actions.c.lease_owner == action["lease_owner"])
                            .values(version=action["version"] + 1, updated_at=self.clock(), **values))
        if result.rowcount != 1:
            raise LeaseLost(action["id"])
        action["version"] += 1

    def save_result(self, action: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Persist intermediate facts (e.g. the pre-change baseline) before touching the target,
        so a crash-and-retry does not mistake our own grant for the user's original access."""
        with self.engine.begin() as db:
            self._action_cas(db, action, result=result)
        action["result"] = result

    def apply_actions(self, approval_id: str) -> List[Dict[str, Any]]:
        with self.engine.connect() as db:
            return [dict(r) for r in db.execute(select(actions).where(actions.c.approval_id == approval_id, actions.c.kind == "apply").order_by(actions.c.seq)).mappings()]

    def succeed(self, action: Dict[str, Any], result: Dict[str, Any], *, preexisting: bool = False) -> None:
        now = self.clock()
        with self.engine.begin() as db:
            self._action_cas(db, action, status="succeeded", result=result, error=None, lease_owner=None, lease_until=None)
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
                        "role": args["role"], "baseline_level": baseline}, adapter=action["adapter"], status="queued", attempts=0,
                        not_before=expires_at, version=0, parent_action_id=action["id"], created_at=now, updated_at=now))
                db.execute(insert(access_grants).values(
                    action_id=action["id"], approval_id=action["approval_id"], employee_id=args["employee_id"], system=args["system"],
                    role=args["role"], adapter=action["adapter"], baseline_role=baseline, status="preexisting" if preexisting else "active",
                    expires_at=expires_at, revoke_action_id=revoke_id, updated_at=now))
            elif action["kind"] == "revoke":
                db.execute(update(access_grants).where(access_grants.c.revoke_action_id == action["id"]).values(status="revoked", updated_at=now))
            event = f"{action['kind']}_succeeded" + ("_preexisting" if preexisting else "")
            self._audit(db, action["approval_id"], event, action["lease_owner"], action_id=action["id"], detail=result)
            self._refresh_execution(db, action["approval_id"])

    def retry_later(self, action: Dict[str, Any], status: str, error: str, delay_s: float) -> None:
        """status=queued for transient failures, unknown when the outcome must be reconciled first."""
        with self.engine.begin() as db:
            self._action_cas(db, action, status=status, error=error[:500], not_before=self.clock() + delay_s, lease_owner=None, lease_until=None)
            self._audit(db, action["approval_id"], f"{action['kind']}_{status}", action["lease_owner"], action_id=action["id"], detail={"error": error[:500]})

    def fail(self, action: Dict[str, Any], error: str, *, status: str = "failed", ledger_status: Optional[str] = None) -> None:
        now = self.clock()
        with self.engine.begin() as db:
            self._action_cas(db, action, status=status, error=error[:500], lease_owner=None, lease_until=None)
            self._audit(db, action["approval_id"], f"{action['kind']}_{status}", action["lease_owner"], action_id=action["id"], detail={"error": error[:500]})
            if action["kind"] == "revoke":
                db.execute(update(access_grants).where(access_grants.c.revoke_action_id == action["id"])
                           .values(status=ledger_status or "revoke_failed", updated_at=now))
            else:
                self._compensate(db, action, now)
            self._refresh_execution(db, action["approval_id"])

    def _compensate(self, db, failed: Dict[str, Any], now: float) -> None:
        """Saga step: cancel the rest of the batch and revoke grants it already made."""
        db.execute(update(actions).where(actions.c.approval_id == failed["approval_id"], actions.c.kind == "apply",
                                         actions.c.seq > failed["seq"], actions.c.status == "queued")
                   .values(status="cancelled", version=actions.c.version + 1, updated_at=now))
        done = db.execute(select(actions).where(actions.c.approval_id == failed["approval_id"], actions.c.kind == "apply",
                                                actions.c.seq < failed["seq"], actions.c.status == "succeeded")).mappings().all()
        for row in done:
            revoke = db.execute(update(actions).where(actions.c.parent_action_id == row["id"], actions.c.status == "queued")
                                .values(not_before=now, version=actions.c.version + 1, updated_at=now))
            event = "compensation_scheduled" if revoke.rowcount else "compensation_unavailable"
            self._audit(db, failed["approval_id"], event, "system", action_id=row["id"], detail={"tool": row["tool_name"]})

    def _refresh_execution(self, db, approval_id: str) -> None:
        rows = db.execute(select(actions.c.status, actions.c.tool_name).where(actions.c.approval_id == approval_id, actions.c.kind == "apply")).all()
        statuses = [r.status for r in rows]
        if not statuses:
            return
        if all(s == "succeeded" for s in statuses):
            state = "succeeded"
        elif any(s not in TERMINAL for s in statuses):
            state = "queued" if all(s == "queued" for s in statuses) else "executing"
        elif "manual_review" in statuses or any(r.status == "succeeded" and r.tool_name != "request_access" for r in rows):
            # A failure after a non-reversible effect (password reset, ticket) needs a human.
            state = "manual_review"
        else:
            state = "failed"
        db.execute(update(approvals).where(approvals.c.id == approval_id).values(execution=state))

    def pending_work(self, approval_id: Optional[str] = None) -> int:
        query = select(func.count()).select_from(actions).where(actions.c.status.in_(("queued", "executing", "unknown")), actions.c.not_before <= self.clock())
        if approval_id:
            query = query.where(actions.c.approval_id == approval_id)
        with self.engine.connect() as db:
            return db.execute(query).scalar()

    # ── Read models ─────────────────────────────────────────────────────────

    def employee(self, employee_id: str) -> Optional[Dict[str, Any]]:
        with self.engine.connect() as db:
            row = db.execute(select(directory_employees).where(directory_employees.c.id == employee_id)).mappings().first()
            if not row:
                return None
            grants = db.execute(select(directory_grants).where(directory_grants.c.employee_id == employee_id).order_by(directory_grants.c.system)).mappings().all()
            tickets = db.execute(select(directory_tickets.c.id, directory_tickets.c.title).where(directory_tickets.c.employee_id == employee_id)).all()
            ledger = db.execute(select(access_grants).where(access_grants.c.employee_id == employee_id).order_by(access_grants.c.updated_at)).mappings().all()
        return {
            "employee_id": row["id"], "username": row["username"], "department": row["department"], "active": row["active"],
            "locked": row["locked"], "password_version": row["password_version"],
            "grants": [{"system": g["system"], "role": g["role"], "operation_id": g["operation_id"]} for g in grants],
            "tickets": [{"ticket_id": t.id, "title": t.title} for t in tickets],
            "access_ledger": [{"action_id": g["action_id"], "approval_id": g["approval_id"], "system": g["system"], "role": g["role"],
                               "adapter": g["adapter"], "status": g["status"], "baseline_role": g["baseline_role"],
                               "expires_at": _iso(g["expires_at"])} for g in ledger],
        }

    def audit(self, approval_id: str) -> List[Dict[str, Any]]:
        with self.engine.connect() as db:
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
