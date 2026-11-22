from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, insert, select, update

from it_copilot.infra import db as schema
from it_copilot.infra.db import (
    access_grants,
    actions,
    approvals,
    audit,
)
from it_copilot.infra.identity import Principal
from it_copilot.workflow.adapters.base import rank
from it_copilot.workflow.repository import (
    ApprovalForbidden,
    ApprovalStateConflict,
    Repository,
    _iso,
    _Retry,
)
from it_copilot.workflow.states import (
    ACTION,
    GRANT,
    OPEN,
    ActionStatus,
    ApprovalStatus,
    Execution,
    GrantStatus,
)


class GrantLedger(Repository):
    def record_grant(self, db, action, result, preexisting, now):
        args = action["arguments"]
        baseline = result["baseline_level"]
        expires_at = now + int(args["duration_hours"]) * 3600
        revoke_id = None
        if not preexisting:
            revoke_id = hashlib.sha256(f"{action['id']}:revoke".encode()).hexdigest()
            db.execute(insert(actions).values(
                id=revoke_id, tenant=action["tenant"], approval_id=action["approval_id"], seq=1000 + action["seq"], kind="revoke",
                tool_name="revoke_access", arguments={"employee_id": args["employee_id"], "system": args["system"],
                "role": args["role"]}, resource_key=action["resource_key"], adapter=action["adapter"], status=ActionStatus.QUEUED, attempts=0,
                not_before=expires_at, version=0, parent_action_id=action["id"], created_at=now, updated_at=now))
            self.store.notifier.publish(db, revoke_id)
        db.execute(insert(access_grants).values(
            action_id=action["id"], tenant=action["tenant"], approval_id=action["approval_id"], employee_id=args["employee_id"], system=args["system"],
            role=args["role"], adapter=action["adapter"], baseline_role=baseline, status=GrantStatus.PREEXISTING if preexisting else GrantStatus.ACTIVE,
            expires_at=expires_at, revoke_action_id=revoke_id, updated_at=now))

    def release(self, approval_id: str, principal: Principal) -> Optional[Dict[str, Any]]:
        """Give back the time-limited access an approval granted before it expires.

        Nothing is revoked here: the revoke action queued when the grant took effect is
        simply made due now, so the worker applies the same drift check as at expiry and
        never removes access the employee had before or someone else changed since.
        Returns the approval with ``released`` listing the grants made due by this call.
        """
        now = self.store.clock()
        with self.store.transaction() as db:
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
                self.store.notifier.publish(db, grant["revoke_action_id"])
                db.execute(update(access_grants).where(access_grants.c.action_id == grant["action_id"])
                           .values(expires_at=now, updated_at=now))
                self.store._audit(db, approval_id, "grant_released", principal.subject, action_id=grant["action_id"],
                            detail={"system": grant["system"], "role": grant["role"], "scheduled_expiry": _iso(grant["expires_at"])})
                released.append(grant["action_id"])
        body = self.store.approvals.get(approval_id)
        return {**body, "released": released}

    def revalidate(self, tenant: Optional[str] = None) -> Dict[str, Any]:
        """Re-check open work against the current policy; ``tenant`` limits the sweep to one tenant."""
        policy, now = self.store.policy, self.store.clock()
        report: Dict[str, Any] = {"policy_version": policy.version, "invalidated": [], "quorum_raised": [], "cancelled": [],
                                  "grants_revoked": [], "grants_shortened": []}
        stale = approvals.c.policy_version != policy.version
        if tenant is not None:
            stale = and_(stale, approvals.c.tenant == tenant)
        with schema.read(self.store.engine) as db:
            pending = db.execute(select(approvals.c.id).where(stale, approvals.c.status.in_(OPEN))).scalars().all()
            queued = db.execute(select(approvals.c.id).where(stale, approvals.c.status == ApprovalStatus.APPROVED,
                                                             approvals.c.execution.in_((Execution.QUEUED, Execution.EXECUTING)))).scalars().all()
            grants = db.execute(select(access_grants.c.action_id).join(approvals, approvals.c.id == access_grants.c.approval_id)
                                .where(stale, access_grants.c.status == GrantStatus.ACTIVE)).scalars().all()
        for approval_id in pending:
            try:
                with self.store.transaction() as db:
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
            with self.store.transaction() as db:
                if self._cancel_if_disallowed(db, approval_id, now):
                    report["cancelled"].append(approval_id)
        for grant_id in grants:
            with self.store.transaction() as db:
                outcome = self._recheck_grant(db, grant_id, now)
                if outcome:
                    report[outcome].append(grant_id)
        return report

    def _revalidate_pending(self, db, row, now):
        """Returns the refreshed row, or None when the approval was invalidated."""
        policy = self.store.policy
        frozen = row["payload"]["action_requests"]
        reason = policy.still_allows(frozen, self.store.approvals._requester(row))
        if reason:
            self.store.approvals._cas(db, row, status=ApprovalStatus.INVALIDATED, decided_at=now)
            self.store._audit(db, row["id"], "policy_invalidated", "system", detail={"from": row["policy_version"], "to": policy.version, "reason": reason})
            self.store._notify(db, row["id"], "invalidated", {"reason": reason})
            return None
        required = max(policy.requirement(a).approvals for a in frozen)
        if required > row["approvals_required"]:
            self.store.approvals._cas(db, row, approvals_required=required)
            self.store._audit(db, row["id"], "policy_quorum_raised", "system",
                        detail={"from": row["policy_version"], "to": policy.version, "required": [row["approvals_required"], required]})
        return db.execute(select(approvals).where(approvals.c.id == row["id"])).mappings().first()

    def _cancel_if_disallowed(self, db, approval_id: str, now: float) -> bool:
        row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
        policy = self.store.policy
        reason = policy.still_allows(row["payload"]["action_requests"], self.store.approvals._requester(row))
        if not reason:
            return False
        # Only actions nobody started; one already running is stopped by the worker's own check.
        cancelled = db.execute(update(actions).where(actions.c.approval_id == approval_id, actions.c.kind == "apply",
                                                     actions.c.status == ActionStatus.QUEUED, actions.c.attempts == 0)
                               .values(status=ActionStatus.CANCELLED, error=reason[:500], version=actions.c.version + 1, updated_at=now)).rowcount
        if not cancelled:
            return False
        self.store._audit(db, approval_id, "policy_cancelled", "system", detail={"to": policy.version, "reason": reason, "actions": cancelled})
        self.store.actions.transactions._refresh_execution(db, approval_id)
        return True

    def _recheck_grant(self, db, grant_id: str, now: float) -> Optional[str]:
        grant = db.execute(select(access_grants).where(access_grants.c.action_id == grant_id)).mappings().first()
        if grant["status"] != GrantStatus.ACTIVE or not grant["revoke_action_id"]:
            return None
        policy = self.store.policy
        max_hours = policy.max_hours(grant["system"], grant["role"])
        approval = db.execute(select(approvals).where(approvals.c.id == grant["approval_id"])).mappings().first()
        if max_hours is None or not self.store.approvals._may_have_filed(grant["employee_id"], approval):
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
        self.store.notifier.publish(db, grant["revoke_action_id"])
        db.execute(update(access_grants).where(access_grants.c.action_id == grant_id).values(expires_at=expires_at, updated_at=now))
        self.store._audit(db, grant["approval_id"], "policy_" + outcome.removeprefix("grants_"), "system", action_id=grant_id,
                    detail={"to": policy.version, "system": grant["system"], "role": grant["role"], "expires_at": _iso(expires_at)})
        return outcome

    def policy_block(self, approval_id: str) -> Optional[str]:
        """Checked by the worker right before an approval's first effect."""
        with schema.read(self.store.engine) as db:
            row = db.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
        if not row:
            return None
        policy = self.store.policy
        if row["policy_version"] == policy.version:
            return None
        return policy.still_allows(row["payload"]["action_requests"], self.store.approvals._requester(row))

    @staticmethod
    def _active_grants(db, tenant, employee_id, system):
        return db.execute(select(access_grants).join(actions, actions.c.id == access_grants.c.action_id).where(
            access_grants.c.tenant == tenant, access_grants.c.employee_id == employee_id,
            access_grants.c.system == system, access_grants.c.status == GrantStatus.ACTIVE,
        ).order_by(actions.c.created_at, actions.c.id)).mappings().all()

    def desired_level(self, db, tenant, employee_id, system, *, excluding_action_id=None) -> Optional[str]:
        active = self._active_grants(db, tenant, employee_id, system)
        if not active:
            return None
        levels = [active[0]["baseline_role"]]
        levels.extend(grant["role"] for grant in active if grant["action_id"] != excluding_action_id)
        return max(levels, key=rank)

    def grant_context(self, action, current):
        args = action["arguments"]
        with schema.read(self.store.engine) as db:
            active = self._active_grants(db, action["tenant"], args["employee_id"], args["system"])
            return {"baseline_level": active[0]["baseline_role"] if active else current,
                    "expected_level": self.desired_level(db, action["tenant"], args["employee_id"], args["system"]),
                    "covering": [{"action_id": grant["action_id"], "role": grant["role"]} for grant in active]}

    def revocation_context(self, action):
        args = action["arguments"]
        with self.store.transaction() as db:
            scope = (db, action["tenant"], args["employee_id"], args["system"])
            return {"expected": self.desired_level(*scope),
                    "restore_to": self.desired_level(*scope, excluding_action_id=action["parent_action_id"])}

    def _drift_grants(self, db, active, current, expected, *, reason):
        changed = []
        now = self.store.clock()
        for grant in active:
            GRANT.check(grant["status"], GrantStatus.DRIFTED)
            done = db.execute(update(access_grants).where(access_grants.c.tenant == grant["tenant"],
                access_grants.c.action_id == grant["action_id"], access_grants.c.status == GrantStatus.ACTIVE,
                access_grants.c.updated_at == grant["updated_at"]).values(status=GrantStatus.DRIFTED, updated_at=now)).rowcount
            if done != 1:
                continue
            revoke = db.execute(select(actions).where(actions.c.tenant == grant["tenant"],
                actions.c.id == grant["revoke_action_id"])).mappings().one()
            if revoke["status"] in {ActionStatus.QUEUED, ActionStatus.UNKNOWN}:
                ACTION.check(revoke["status"], ActionStatus.CANCELLED)
                db.execute(update(actions).where(actions.c.tenant == grant["tenant"], actions.c.id == revoke["id"],
                    actions.c.version == revoke["version"]).values(status=ActionStatus.CANCELLED,
                    error=reason, lease_owner=None, lease_until=None,
                    version=revoke["version"] + 1, updated_at=now))
            self.store._audit(db, grant["approval_id"], "grant_drift_detected", "system", action_id=grant["action_id"],
                detail={"system": grant["system"], "employee_id": grant["employee_id"], "expected": expected, "actual": current})
            changed.append(grant["action_id"])
        return changed

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
            if revoke.rowcount:
                self.store.notifier.publish(db, row["id"])
            event = "compensation_scheduled" if revoke.rowcount else "compensation_unavailable"
            self.store._audit(db, failed["approval_id"], event, "system", action_id=row["id"], detail={"tool": row["tool_name"]})

    @staticmethod
    def _manually_resolved(db, approval_id: str) -> bool:
        return db.execute(select(audit.c.id).where(audit.c.approval_id == approval_id,
                                                   audit.c.event == "manual_resolved").limit(1)).first() is not None
