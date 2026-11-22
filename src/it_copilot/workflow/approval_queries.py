from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import and_, false, or_, select

from it_copilot.infra import db as schema
from it_copilot.infra.db import (
    access_grants,
    actions,
    approval_decisions,
    approvals,
    audit,
)
from it_copilot.infra.identity import Principal
from it_copilot.workflow.catalog import resource_key
from it_copilot.workflow.lifecycle import approval_phase, work_phase
from it_copilot.workflow.repository import Repository, _describe, _iso
from it_copilot.workflow.states import OPEN, ActionStatus, ApprovalStatus, Execution, GrantStatus


class ApprovalQueries(Repository):
    def get(self, approval_id: str) -> Optional[Dict[str, Any]]:
        with schema.read(self.store.engine) as db:
            bodies = self._get_many(db, [approval_id])
            return bodies[0] if bodies else None

    def get_many(self, approval_ids):
        """Read complete approval views in one snapshot, sharing conflict lookup per tenant."""
        ids = list(dict.fromkeys(approval_ids))
        if not ids:
            return []
        with schema.read(self.store.engine) as db:
            return self._get_many(db, ids)

    def _get_many(self, db, ids):
        rows = db.execute(select(approvals).where(approvals.c.id.in_(ids))).mappings().all()
        if not rows:
            return []
        decisions = db.execute(select(approval_decisions).where(approval_decisions.c.approval_id.in_(ids))
                               .order_by(approval_decisions.c.id)).mappings().all()
        operations = db.execute(self.store.grants.reviewable_actions().where(actions.c.approval_id.in_(ids))
                                .order_by(actions.c.seq)).mappings().all()
        conflicts = {tenant: self._open_resources(db, tenant) for tenant in {r['tenant'] for r in rows}}
        by_id = {r['id']: r for r in rows}
        grouped_decisions, grouped_operations = {}, {}
        for item in decisions:
            grouped_decisions.setdefault(item['approval_id'], []).append(item)
        for item in operations:
            grouped_operations.setdefault(item['approval_id'], []).append(item)
        history = {}
        if operations:
            reviews = select(audit.c.approval_id, audit.c.event, audit.c.action_id, audit.c.actor, audit.c.detail, audit.c.at).where(
                audit.c.approval_id.in_(ids), audit.c.event.in_(("manual_retry", "manual_resolved", "manual_accept_external")))
            for entry in db.execute(reviews.order_by(audit.c.id)).mappings():
                history.setdefault(entry["approval_id"], []).append({**entry, "at": _iso(entry["at"])})
        return [self._body(by_id[i], grouped_decisions.get(i, []), grouped_operations.get(i, []),
                           conflicts[by_id[i]['tenant']], history.get(i, [])) for i in ids if i in by_id]

    def _body(self, row, decisions, rows, open_resources, history) -> Dict[str, Any]:
        payload = dict(row["payload"])
        now = self.store.clock()

        def view(a):
            blocked = a["kind"] == "apply" and any(
                prior["kind"] == "apply" and prior["seq"] < a["seq"] and prior["status"] != ActionStatus.SUCCEEDED for prior in rows)
            return {"operation_id": a["id"], "version": a["version"], "drifted": a["drifted"], "seq": a["seq"], "kind": a["kind"], "action": a["tool_name"], "adapter": a["adapter"],
                    "status": a["status"], "phase": work_phase(a, now, blocked=blocked), "attempts": a["attempts"], "not_before": _iso(a["not_before"]), "error": a["error"],
                    **{k: v for k, v in (a["result"] or {}).items() if k != "baseline_level"}}

        return {
            **payload, "approval_id": row["id"], "request_id": row["request_id"], "conv_id": row["conv_id"],
            "user_id": row["requester"], "requester": row["requester"], "requester_employee_id": row["requester_employee_id"],
            "department": row["department"], "tenant": row["tenant"], "status": row["status"], "execution": row["execution"],
            "audit": history, "lifecycle": approval_phase(row, decisions, rows, now),
            "payload_hash": row["payload_hash"], "policy_version": row["policy_version"],
            "approvals_required": row["approvals_required"],
            "requester_name": row["requester_name"] or row["requester"],
            "decisions": [{"actor": d["actor"], "actor_name": d["actor_name"] or d["actor"], "decision": d["decision"], "at": _iso(d["at"])} for d in decisions],
            "created_at": _iso(row["created_at"]), "expires_at": _iso(row["expires_at"]), "decided_at": _iso(row["decided_at"]),
            "awaiting_confirmation": row["status"] == ApprovalStatus.DRAFT, "confirmed_at": _iso(row.get("confirmed_at")),
            "results": [view(a) for a in rows if a["kind"] == "apply"],
            "revocations": [view(a) for a in rows if a["kind"] == "revoke"],
            "conflicts": self._conflicts(row, open_resources),
        }

    @staticmethod
    def _resource_keys(row) -> set:
        return {resource_key(row["tenant"], item["tool_name"], {**item["arguments"], "action_id": f"{row['id']}:{i}"})
                for i, item in enumerate(row["payload"]["action_requests"])}

    def _open_resources(self, db, tenant) -> Dict[str, List[Dict[str, Any]]]:
        """resource_key -> in-flight approvals touching it, oldest first; one query per read, not one per approval."""
        index: Dict[str, List[Dict[str, Any]]] = {}
        rows = db.execute(select(approvals.c.id, approvals.c.tenant, approvals.c.status, approvals.c.requester,
                                 approvals.c.conv_id, approvals.c.payload).where(
            approvals.c.tenant == tenant,
            approvals.c.status.in_((ApprovalStatus.DRAFT, ApprovalStatus.PENDING, ApprovalStatus.APPROVED)),
            approvals.c.execution.not_in((Execution.SUCCEEDED, Execution.FAILED)),
        ).order_by(approvals.c.created_at, approvals.c.id)).mappings()
        for order, other in enumerate(rows):
            conv = other["conv_id"]
            channel = "task" if conv and conv.startswith("task:") else "feishu" if conv and conv.startswith("feishu:") else "web"
            summary = {"approval_id": other["id"], "status": other["status"], "requester": other["requester"],
                       "conv_channel": channel, "_order": order}
            for key in self._resource_keys(other):
                index.setdefault(key, []).append(summary)
        return index

    def _conflicts(self, row, open_resources):
        found = {other["approval_id"]: other for key in self._resource_keys(row)
                 for other in open_resources.get(key, ()) if other["approval_id"] != row["id"]}
        return [{k: v for k, v in other.items() if k != "_order"} for other in sorted(found.values(), key=lambda o: o["_order"])]

    def list(self, principal: Principal, status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        private_scope = or_(approvals.c.requester == principal.subject, principal.can_read_all,
            bool(self.store.policy.approver_roles.intersection(principal.roles)))
        shared_scope = and_(approvals.c.tenant == principal.tenant,
            or_(approvals.c.requester == principal.subject, principal.is_admin))
        shared = approvals.c.conv_id.startswith('task:')
        query = select(approvals.c.id).where(approvals.c.tenant == principal.tenant,
            or_(and_(shared, shared_scope), and_(or_(approvals.c.conv_id.is_(None), ~shared), private_scope)))
        # A private draft is the requester's until confirmed: approvers do not see it in their queue.
        # Shared-task drafts stay visible to the room (members see the card waiting on the requester).
        query = query.where(or_(shared, approvals.c.status != ApprovalStatus.DRAFT, approvals.c.requester == principal.subject, principal.can_read_all))
        if status:
            query = query.where(approvals.c.status == status)
        query = query.order_by(approvals.c.created_at.desc()).limit(limit)
        with schema.read(self.store.engine) as db:
            ids = db.execute(query).scalars().all()
            return self._get_many(db, ids)

    def can_read(self, body: Dict[str, Any], principal: Principal) -> bool:
        if body["status"] == ApprovalStatus.DRAFT:  # not yet submitted: the requester's own business
            return self._shared_scope(body, principal) and (body["requester"] == principal.subject or principal.can_read_all)
        return self._shared_scope(body, principal) and (body["requester"] == principal.subject or principal.can_read_all
                or bool(self.store.policy.approver_roles.intersection(principal.roles)))

    def _shared_scope(self, row, principal):
        if row.get('tenant') != principal.tenant:
            return False
        if not (row.get("conv_id") or "").startswith("task:"):
            return True
        return principal.is_admin or row["requester"] == principal.subject

    def conversation_approvals(self, principal: Principal, conv_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Latest persisted states for this caller's requests in this conversation."""
        if not conv_id:
            return []
        with schema.read(self.store.engine) as db:
            rows = db.execute(select(approvals).where(
                approvals.c.tenant == principal.tenant, approvals.c.requester == principal.subject,
                approvals.c.conv_id == conv_id,
            ).order_by(approvals.c.created_at.desc(), approvals.c.id).limit(limit)).mappings().all()
            grants = db.execute(select(access_grants).where(
                access_grants.c.tenant == principal.tenant,
                access_grants.c.approval_id.in_([row["id"] for row in rows]),
            )).mappings().all() if rows else []
        return [{"approval_id": row["id"], "status": row["status"], "execution": row["execution"],
                 "grants": [{k: g[k] for k in ("system", "role", "status", "baseline_role", "expires_at")}
                            for g in grants if g["approval_id"] == row["id"]],
                 "confirmed_at": row["confirmed_at"], "approvals_required": row["approvals_required"],
                 "actions": [a["tool_name"] for a in row["payload"]["action_requests"]],
                 "summary": _describe(row["payload"]["action_requests"])} for row in rows]

    def drafts(self, principal: Principal, conv_id: str) -> List[Dict[str, Any]]:
        """This requester's unconfirmed, unexpired drafts in one conversation, oldest first."""
        with schema.read(self.store.engine) as db:
            ids = db.execute(select(approvals.c.id).where(
                approvals.c.tenant == principal.tenant, approvals.c.requester == principal.subject, approvals.c.conv_id == conv_id,
                approvals.c.status == ApprovalStatus.DRAFT, approvals.c.expires_at > self.store.clock(),
            ).order_by(approvals.c.created_at, approvals.c.id)).scalars().all()
            return [{**body, "description": _describe(body["action_requests"])}
                    for body in self._get_many(db, ids)] if ids else []

    def requester_items(self, principal: Principal, limit: int = 5) -> List[Dict[str, Any]]:
        """What this person could act on right now: own drafts (confirm or cancel), own pending
        requests (withdraw) and live time-limited grants they requested or hold (release). Read-only; the same checks run
        again when the button is pressed."""
        now = self.store.clock()
        with schema.read(self.store.engine) as db:
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

    def audit(self, approval_id: str) -> List[Dict[str, Any]]:
        with schema.read(self.store.engine) as db:
            rows = db.execute(select(audit).where(audit.c.approval_id == approval_id).order_by(audit.c.id)).mappings().all()
        return [{"event": r["event"], "actor": r["actor"], "action_id": r["action_id"], "detail": r["detail"], "at": _iso(r["at"])} for r in rows]
