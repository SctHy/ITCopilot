from __future__ import annotations

from servicedesk_agent.infra.monitor.telemetry import traceparent

import hashlib
from functools import wraps
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError

from servicedesk_agent.infra import db as schema
from servicedesk_agent.infra import admin_audit, fair_queue
from servicedesk_agent.infra.monitor import metrics
from servicedesk_agent.infra.db import (
    actions,
    approvals,
    tenants,
)
from servicedesk_agent.infra.tenant_context import tenant_scope
from servicedesk_agent.workflow.catalog import WRITE_ACTIONS as WRITE_ACTIONS
from servicedesk_agent.workflow.catalog import WRITE_TOOLS as WRITE_TOOLS
from servicedesk_agent.workflow.catalog import resource_key
from servicedesk_agent.workflow.catalog import risk_of as risk_of
from servicedesk_agent.workflow.repository import (
    ApprovalForbidden,
    ApprovalStateConflict,
    LeaseLost,
    Repository,
    _reversible,
    action_id,
)
from servicedesk_agent.workflow.states import (
    ACTION,
    EXECUTION,
    NOTIFIED,
    SETTLED,
    ActionStatus,
    Execution,
    GrantStatus,
)


class ActionTransactions(Repository):
    def _enqueue(self, db, row, now) -> None:
        rows = []
        with tenant_scope(row["tenant"]):  # route to the owning tenant's adapters, whoever decided (ADR 0025)
            routes = [self.store.route(action) for action in row["payload"]["action_requests"]]
        for seq, action in enumerate(row["payload"]["action_requests"]):
            key = action_id(row["id"], row["payload_hash"], seq)
            rows.append({
                "id": key, "traceparent": traceparent(), "tenant": row["tenant"], "approval_id": row["id"], "seq": seq, "kind": "apply",
                "resource_key": resource_key(row["tenant"], action["tool_name"], {**action["arguments"], "action_id": key}),
                "tool_name": action["tool_name"], "arguments": action["arguments"], "adapter": routes[seq],
                "status": ActionStatus.QUEUED, "attempts": 0, "not_before": now, "version": 0, "created_at": now, "updated_at": now,
            })
        db.execute(insert(actions), rows)
        self.store.notifier.publish(db, row["id"])

    def requeue(self, approval_id, principal, reason):
        return self._review_batch(approval_id, principal, "retry", reason)

    def resolve_manual(self, approval_id, principal, note):
        return self._review_batch(approval_id, principal, "mark_resolved", note)

    def _review_batch(self, approval_id, principal, resolution, reason):
        with self.store.transaction() as db:
            approval = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
            if not approval:
                return None
            if not principal.is_admin or principal.tenant != approval["tenant"]:
                raise ApprovalForbidden("无权处理该审批")
            rows = db.execute(select(actions).where(actions.c.approval_id == approval_id,
                actions.c.status.in_((ActionStatus.MANUAL_REVIEW, ActionStatus.UNKNOWN)))
                .order_by(actions.c.seq)).mappings().all()
            if not rows:
                raise ApprovalStateConflict("没有需要人工核对的动作")
            for row in rows:
                self._write_review(db, row, principal, resolution, reason, None)
        metrics.MANUAL_RESOLUTIONS.labels(resolution).inc(len(rows))
        return self.store.get(approval_id)

    def review_action(self, approval_id, operation_id, principal, version, resolution):
        if not principal.is_admin:
            raise ApprovalForbidden("需要管理员权限")
        with schema.read(self.store.engine) as db:
            row = db.execute(self.store.grants.reviewable_actions().where(actions.c.id == operation_id,
                actions.c.approval_id == approval_id, actions.c.tenant == principal.tenant)).mappings().first()
        if not row:
            return None
        drift_closure = row["drifted"] and row["status"] == ActionStatus.CANCELLED and resolution != "retry"
        if row["version"] != version or not (drift_closure or row["status"] in {ActionStatus.UNKNOWN, ActionStatus.MANUAL_REVIEW}):
            raise ApprovalStateConflict("动作状态已变化，请刷新后重试")
        return dict(row)

    def commit_review(self, row, principal, resolution, reason, note, *, observed=None, preexisting=False):
        with self.store.transaction() as db:
            self._write_review(db, row, principal, resolution, reason, note, observed=observed, preexisting=preexisting)
        metrics.MANUAL_RESOLUTIONS.labels(resolution).inc()
        return self.store.get(row["approval_id"])

    def _write_review(self, db, row, principal, resolution, reason, note, *, observed=None, preexisting=False):
        if not principal.is_admin or principal.tenant != row["tenant"]:
            raise ApprovalForbidden("无权处理该动作")
        if not reason.strip():
            raise ValueError("必须填写处置原因")
        target = {"retry": ActionStatus.UNKNOWN, "mark_resolved": ActionStatus.CANCELLED,
                  "accept_external": ActionStatus.SUCCEEDED}[resolution]
        result = dict(row["result"] or {})
        now = self.store.clock()
        values = {}
        if resolution == "retry":
            values.update(attempts=0, not_before=now)
        elif resolution == "accept_external":
            if observed is None:
                raise ApprovalStateConflict("外部状态未确认")
            if not observed or row["status"] == ActionStatus.CANCELLED:
                target = ActionStatus.CANCELLED
            result["reconciled"] = True
            values.update(result=result, error=None)
        else:
            values["error"] = (note or reason)[:500]
        ACTION.check(row["status"], target)
        changed = db.execute(update(actions).where(actions.c.id == row["id"], actions.c.tenant == principal.tenant,
            actions.c.version == row["version"], actions.c.status == row["status"]).values(status=target,
            version=row["version"] + 1, updated_at=now, **values)).rowcount
        if changed != 1:
            raise ApprovalStateConflict("动作状态已变化，请刷新后重试")
        if resolution != "retry":
            if row["kind"] == "revoke":
                status = GrantStatus.REVOKED if observed else GrantStatus.RESOLVED
                self.store.grants._settle_grant(db, row["id"], status, now)
            elif observed and row["tool_name"] == "request_access":
                self.store.grants.record_grant(db, row, result, preexisting, now)
            elif not observed:
                self.store.grants._compensate(db, row, now)
        event = "manual_resolved" if resolution == "mark_resolved" else "manual_" + resolution
        detail = {"resolution": resolution, "reason": reason, "resolution_note": note, "version": row["version"]}
        self.store._audit(db, row["approval_id"], event, principal.subject, action_id=row["id"], detail=detail)
        admin_audit.append(db, tenant=principal.tenant, actor=principal.subject, event=event, subject=row["id"],
                           detail={**detail, "approval_id": row["approval_id"]}, at=now)
        self._refresh_execution(db, row["approval_id"])
        self.store.notifier.publish(db, row["id"])

    def claim(self, worker_id: str, lease_s: float, limit: int = 10, *, exclude_ids=(),
              after_tenant: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lease due actions. Tenants are served in turn (``servicedesk_agent.infra.fair_queue``, ADR 0035); ``after_tenant``
        is the tenant the caller served last."""
        now = self.store.clock()
        due = or_(and_(actions.c.status.in_((ActionStatus.QUEUED, ActionStatus.UNKNOWN)), actions.c.not_before <= now),
                  and_(actions.c.status == ActionStatus.EXECUTING, actions.c.lease_until < now))
        earlier = actions.alias("earlier_action")
        blocked = select(earlier.c.id).where(
            earlier.c.approval_id == actions.c.approval_id, earlier.c.kind == "apply",
            earlier.c.seq < actions.c.seq, earlier.c.status != ActionStatus.SUCCEEDED,
        ).exists()
        due = and_(due, or_(actions.c.kind != "apply", ~blocked))
        holder = actions.alias("resource_holder")
        same_resource = (holder.c.tenant == actions.c.tenant, holder.c.resource_key == actions.c.resource_key,
                         holder.c.id != actions.c.id)
        reserved = select(holder.c.id).where(*same_resource,
            holder.c.status.in_((ActionStatus.EXECUTING, ActionStatus.UNKNOWN, ActionStatus.MANUAL_REVIEW))).exists()
        older_unknown = and_(holder.c.status == ActionStatus.UNKNOWN,
            or_(holder.c.created_at < actions.c.created_at,
                and_(holder.c.created_at == actions.c.created_at, holder.c.id < actions.c.id)))
        recovery_blocked = select(holder.c.id).where(*same_resource,
            or_(holder.c.status.in_((ActionStatus.EXECUTING, ActionStatus.MANUAL_REVIEW)), older_unknown)).exists()
        manual_blocked = select(holder.c.id).where(*same_resource, holder.c.status == ActionStatus.MANUAL_REVIEW).exists()
        due = and_(due, or_(and_(actions.c.status == ActionStatus.QUEUED, ~reserved),
            and_(actions.c.status == ActionStatus.UNKNOWN, ~recovery_blocked),
            and_(actions.c.status == ActionStatus.EXECUTING, ~manual_blocked)))
        # Suspension stops new grants, not expiry/compensation of existing grants.
        due = and_(due, or_(actions.c.kind == "revoke",
            actions.c.tenant.not_in(select(tenants.c.id).where(tenants.c.status != "active"))))
        if limit <= 0:
            return []
        claimed = []
        seen = set(exclude_ids)
        with self.store.transaction() as db:
            if db.dialect.name == "postgresql":
                while len(claimed) < limit:
                    stmt = self.lock_next(due, seen, after_tenant)
                    row = db.execute(stmt).mappings().first()
                    if row is None:
                        break
                    seen.add(row["id"])
                    key = int.from_bytes(hashlib.sha256(row["resource_key"].encode()).digest()[:8], signed=True)
                    if db.execute(select(func.pg_try_advisory_xact_lock(key))).scalar():
                        action = self._claim_row(db, row, worker_id, lease_s, now)
                        if action:
                            claimed.append(action)
                            after_tenant = row["tenant"]
            else:
                candidates = self._candidates(db, due, seen, limit)
                for row in fair_queue.rotate(candidates, after_tenant):
                    action = self._claim_row(db, fair_queue.strip_rank(row), worker_id, lease_s, now)
                    if action:
                        claimed.append(action)
                    if len(claimed) >= limit:
                        break
        for item in claimed:
            due_at = item['lease_until'] if item['prev_status'] == ActionStatus.EXECUTING else item['not_before']
            metrics.DISPATCH_LATENCY.observe(max(0, now - due_at))
        return claimed

    @staticmethod
    def lock_next(due, excluded, after_tenant):
        order = [] if after_tenant is None else [actions.c.tenant <= after_tenant, actions.c.tenant]
        return (select(actions).where(due, actions.c.id.not_in(excluded))
                .order_by(*order, actions.c.not_before, actions.c.seq, actions.c.id)
                .limit(1).with_for_update(skip_locked=True))

    @staticmethod
    def _candidates(db, due, exclude_ids, limit):
        ranked = select(actions, fair_queue.tenant_rank(actions.c.tenant, actions.c.not_before, actions.c.seq)).where(
            due, actions.c.id.not_in(exclude_ids)).subquery()
        candidates = db.execute(select(ranked).where(ranked.c.tenant_rank <= limit * 4)
                                    .order_by(ranked.c.tenant_rank, ranked.c.not_before, ranked.c.seq).limit(limit * 4)).mappings().all()
        return candidates

    def _claim_row(self, db, row, worker_id, lease_s, now):
        unsettled = db.execute(select(actions.c.id, actions.c.status, actions.c.created_at).where(
            actions.c.tenant == row["tenant"], actions.c.resource_key == row["resource_key"], actions.c.id != row["id"],
            actions.c.status.in_((ActionStatus.EXECUTING, ActionStatus.UNKNOWN, ActionStatus.MANUAL_REVIEW)))).mappings().all()
        if row["status"] == ActionStatus.QUEUED and unsettled:
            return None
        if row["status"] == ActionStatus.EXECUTING and any(other["status"] == ActionStatus.MANUAL_REVIEW for other in unsettled):
            return None
        if row["status"] == ActionStatus.UNKNOWN and any(
            other["status"] in {ActionStatus.EXECUTING, ActionStatus.MANUAL_REVIEW} or
            (other["created_at"], other["id"]) < (row["created_at"], row["id"]) for other in unsettled):
            return None
        if row["kind"] == "apply" and db.execute(select(actions.c.id).where(
                actions.c.approval_id == row["approval_id"], actions.c.kind == "apply",
                actions.c.seq < row["seq"], actions.c.status != ActionStatus.SUCCEEDED)).first():
            return None  # apply actions of one approval run strictly in order
        ACTION.check(row["status"], ActionStatus.EXECUTING)
        parent = row["traceparent"] or db.scalar(select(approvals.c.traceparent).where(approvals.c.id == row["approval_id"])) or traceparent()
        try:
            with db.begin_nested():
                result = db.execute(update(actions).where(actions.c.id == row["id"], actions.c.version == row["version"]).values(
                    status=ActionStatus.EXECUTING, traceparent=parent, lease_owner=worker_id, lease_until=now + lease_s, attempts=row["attempts"] + 1,
                    version=row["version"] + 1, updated_at=now))
        except IntegrityError as exc:
            if "uq_actions_resource_executing" not in str(exc.orig) and "actions.resource_key" not in str(exc.orig):
                raise
            return None
        if result.rowcount != 1:
            return None  # another worker won the CAS
        if row["kind"] == "apply" and row["seq"] == 0 and row["attempts"] == 0:
            db.execute(update(approvals).where(approvals.c.id == row["approval_id"], approvals.c.execution == Execution.QUEUED).values(execution=Execution.EXECUTING))
        self.store._audit(db, row["approval_id"], f"{row['kind']}_started", worker_id, action_id=row["id"], detail={"attempt": row["attempts"] + 1, "previous": row["status"]})
        return {**row, "traceparent": parent, "prev_status": row["status"], "status": ActionStatus.EXECUTING,
                "attempts": row["attempts"] + 1, "version": row["version"] + 1, "lease_owner": worker_id}

    def release_lease(self, action, *, uncertain=True, reason='shutdown'):
        """Make interrupted work eligible immediately. Observe ambiguous effects before replay."""
        if reason not in {'shutdown', 'error'}:
            raise ValueError('invalid lease release reason')
        status = ActionStatus.UNKNOWN if uncertain else action["prev_status"]
        if status == ActionStatus.EXECUTING:
            status = ActionStatus.UNKNOWN
        with self.store.transaction() as db:
            # A cancellation can interrupt commit after an in-memory version was advanced.
            # Fence on the durable ownership epoch and reconcile the actual row.
            released = db.execute(update(actions).where(actions.c.id == action["id"],
                actions.c.lease_owner == action["lease_owner"], actions.c.attempts == action["attempts"],
                actions.c.version <= action["version"], actions.c.status == ActionStatus.EXECUTING, actions.c.lease_until >= self.store.clock()).values(
                    status=status, not_before=self.store.clock(), lease_owner=None, lease_until=None,
                    version=actions.c.version + 1, updated_at=self.store.clock()))
            if not released.rowcount:
                raise LeaseLost(action["id"])
            self.store.notifier.publish(db, action["id"])

        metrics.WORKFLOW_RELEASES.labels(reason).inc()

    def renew(self, action, lease_s):
        """Attempts is the claim epoch; intermediate result writes may change version."""
        now = self.store.clock()
        with self.store.transaction() as db:
            return db.execute(update(actions).where(
                actions.c.id == action["id"], actions.c.lease_owner == action["lease_owner"],
                actions.c.attempts == action["attempts"], actions.c.status == ActionStatus.EXECUTING,
                actions.c.lease_until >= now,
            ).values(lease_until=now + lease_s)).rowcount == 1

    def _action_cas(self, db, action, **values) -> None:
        if "status" in values:
            ACTION.check(action["status"], values["status"])
        result = db.execute(update(actions).where(actions.c.id == action["id"], actions.c.version == action["version"],
                                                  actions.c.lease_owner == action["lease_owner"], actions.c.lease_until >= self.store.clock())
                            .values(version=action["version"] + 1, updated_at=self.store.clock(), **values))
        if result.rowcount != 1:
            raise LeaseLost(action["id"])
        action["version"] += 1
        action["status"] = values.get("status", action["status"])

    def save_result(self, action: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Persist intermediate facts (e.g. the pre-change baseline) before touching the target,
        so a crash-and-retry does not mistake our own grant for the user's original access."""
        with self.store.transaction() as db:
            self._action_cas(db, action, result=result)
        action["result"] = result

    def apply_actions(self, approval_id: str) -> List[Dict[str, Any]]:
        with schema.read(self.store.engine) as db:
            return [dict(r) for r in db.execute(select(actions).where(actions.c.approval_id == approval_id, actions.c.kind == "apply").order_by(actions.c.seq)).mappings()]

    def succeed(self, action: Dict[str, Any], result: Dict[str, Any], *, preexisting: bool = False) -> None:
        now = self.store.clock()
        with self.store.transaction() as db:
            self._action_cas(db, action, status=ActionStatus.SUCCEEDED, result=result, error=None, lease_owner=None, lease_until=None)
            if action["kind"] == "apply" and action["tool_name"] == "request_access":
                self.store.grants.record_grant(db, action, result, preexisting, now)
            elif action["kind"] == "revoke":
                self.store.grants._settle_grant(db, action["id"], GrantStatus.REVOKED, now)
            event = f"{action['kind']}_succeeded" + ("_preexisting" if preexisting else "")
            self.store._audit(db, action["approval_id"], event, action["lease_owner"], action_id=action["id"], detail=result)
            self._refresh_execution(db, action["approval_id"])

    def retry_later(self, action: Dict[str, Any], status: str, error: str, delay_s: float) -> None:
        """status=queued for transient failures, unknown when the outcome must be reconciled first."""
        with self.store.transaction() as db:
            self._action_cas(db, action, status=status, error=error[:500], not_before=self.store.clock() + delay_s, lease_owner=None, lease_until=None)
            self.store.notifier.publish(db, action["id"])
            self.store._audit(db, action["approval_id"], f"{action['kind']}_{status}", action["lease_owner"], action_id=action["id"], detail={"error": error[:500]})

    def fail(self, action: Dict[str, Any], error: str, *, status: str = ActionStatus.FAILED, ledger_status: Optional[str] = None,
             resource_drift=None) -> None:
        now = self.store.clock()
        with self.store.transaction() as db:
            self._action_cas(db, action, status=status, error=error[:500], lease_owner=None, lease_until=None)
            self.store.notifier.publish(db, action["id"])
            self.store._audit(db, action["approval_id"], f"{action['kind']}_{status}", action["lease_owner"], action_id=action["id"], detail={"error": error[:500]})
            if resource_drift is not None:
                args = action["arguments"]
                active = self.store.grants._active_grants(db, action["tenant"], args["employee_id"], args["system"])
                self.store.grants._drift_grants(db, active, resource_drift["current"], resource_drift["expected"], reason="授权前发现漂移")
            if action["kind"] == "revoke":
                self.store.grants._settle_grant(db, action["id"], ledger_status or GrantStatus.REVOKE_FAILED, now)
            else:
                self.store.grants._compensate(db, action, now)
            self._refresh_execution(db, action["approval_id"])

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
        elif ActionStatus.MANUAL_REVIEW in statuses or (any(r.status == ActionStatus.SUCCEEDED and not _reversible(r.tool_name) for r in rows)
                                                        and not self.store.grants._manually_resolved(db, approval_id)):
            # A failure after a non-reversible effect (password reset, ticket) needs a human.
            state = Execution.MANUAL_REVIEW
        else:
            state = Execution.FAILED
        EXECUTION.check(previous, state)
        db.execute(update(approvals).where(approvals.c.id == approval_id).values(execution=state))
        if state != previous and state in NOTIFIED:
            done = db.execute(select(actions.c.result, actions.c.error).where(actions.c.approval_id == approval_id, actions.c.kind == "apply")
                              .order_by(actions.c.seq)).all()
            self.store._notify(db, approval_id, "execution_" + state, {"error": next((a.error for a in done if a.error), None),
                                                                 "results": [a.result for a in done if a.result]})

    def pending_work(self, approval_id: Optional[str] = None) -> int:
        query = select(func.count()).select_from(actions).where(actions.c.status.in_((ActionStatus.QUEUED, ActionStatus.EXECUTING, ActionStatus.UNKNOWN)), actions.c.not_before <= self.store.clock())
        if approval_id:
            query = query.where(actions.c.approval_id == approval_id)
        with schema.read(self.store.engine) as db:
            return db.execute(query).scalar()

    def action_ages(self):
        now = self.store.clock()
        with schema.read(self.store.engine) as db:
            unknown = db.execute(select(func.min(actions.c.updated_at)).where(
                actions.c.status.in_((ActionStatus.UNKNOWN, ActionStatus.MANUAL_REVIEW)))).scalar()
            queued = db.execute(select(func.min(actions.c.not_before)).where(
                actions.c.status == ActionStatus.QUEUED, actions.c.not_before <= now)).scalar()
        return {"oldest_unknown_action_age_s": max(0, now - unknown) if unknown is not None else 0,
                "oldest_queued_due_action_age_s": max(0, now - queued) if queued is not None else 0}

def async_transaction(operation):
    """Expose a Core transaction using the queue's async driver."""
    @wraps(operation)
    async def run(queue, *args, **kwargs):
        return await queue.store.run(operation, queue.transactions, *args, **kwargs)
    return run


class ActionQueue(Repository):
    def __init__(self, store):
        super().__init__(store)
        self.transactions = ActionTransactions(store)

    claim = async_transaction(ActionTransactions.claim)
    release_lease = async_transaction(ActionTransactions.release_lease)
    renew = async_transaction(ActionTransactions.renew)
    save_result = async_transaction(ActionTransactions.save_result)
    apply_actions = async_transaction(ActionTransactions.apply_actions)
    succeed = async_transaction(ActionTransactions.succeed)
    retry_later = async_transaction(ActionTransactions.retry_later)
    fail = async_transaction(ActionTransactions.fail)
    pending_work = async_transaction(ActionTransactions.pending_work)
    action_ages = async_transaction(ActionTransactions.action_ages)
    requeue = async_transaction(ActionTransactions.requeue)
    resolve_manual = async_transaction(ActionTransactions.resolve_manual)
