"""Compare live entitlements with the ledger without changing the target system."""
import asyncio

from sqlalchemy import and_, or_, select

from core import db as schema
from core.db import access_grants, actions, approvals, audit
from core.tenant_context import system_scope, tenant_scope
from monitor import metrics
from workflow.actions import resource_key
from workflow.adapters.base import AmbiguousError, TransientError
from workflow.states import ActionStatus, GrantStatus


class GrantReconciler:
    def __init__(self, store, registry, tasks=None):
        self.store, self.registry, self.tasks = store, registry, tasks

    async def run_once(self):
        with system_scope():
            tenants = await asyncio.to_thread(self._tenants)
        drifted = 0
        for tenant in tenants:
            with tenant_scope(tenant):
                cursor = ""
                while True:
                    batch = await asyncio.to_thread(self._batch, tenant, cursor)
                    if not batch:
                        break
                    for grant in batch:
                        cursor = grant["action_id"]
                        snapshot = await asyncio.to_thread(self._snapshot, grant)
                        if not snapshot:
                            continue
                        adapter = self.registry.by_name(grant["adapter"])
                        try:
                            current = await adapter.current_level(grant["employee_id"], grant["system"])
                        except (TransientError, AmbiguousError):
                            metrics.RECONCILE_SKIPPED.inc()
                            continue
                        expected = snapshot["expected"]
                        expected = adapter.level(expected) if expected is not None else None
                        if current != expected:
                            changed = await asyncio.to_thread(self._mark, grant, snapshot, current, expected)
                            drifted += len(changed)
                            if changed:
                                metrics.GRANT_DRIFT.labels(system=grant["system"]).inc()
                if self.tasks is not None:
                    await asyncio.to_thread(self._deliver, tenant)
        return drifted

    def _tenants(self):
        with schema.read(self.store.engine) as db:
            return list(db.execute(select(access_grants.c.tenant).distinct().where(
                access_grants.c.status == GrantStatus.ACTIVE).union(
                    select(audit.c.tenant).distinct().where(audit.c.event == "grant_drift_detected"))).scalars())

    def _batch(self, tenant, cursor):
        with schema.read(self.store.engine) as db:
            return [dict(row) for row in db.execute(select(access_grants).where(
                access_grants.c.tenant == tenant, access_grants.c.status == GrantStatus.ACTIVE,
                access_grants.c.action_id > cursor).order_by(access_grants.c.action_id).limit(50)).mappings()]

    @staticmethod
    def _signature(grants):
        return [(row["action_id"], row["updated_at"]) for row in grants]

    def _executing(self, db, grant):
        key = resource_key(grant["tenant"], "request_access", grant)
        # Due/unknown effects must run or reconcile first; their own in-flight change is not external drift.
        return db.execute(select(actions.c.id).where(actions.c.tenant == grant["tenant"],
            actions.c.resource_key == key, or_(actions.c.status.in_((ActionStatus.EXECUTING, ActionStatus.UNKNOWN, ActionStatus.MANUAL_REVIEW)),
                and_(actions.c.status == ActionStatus.QUEUED, actions.c.not_before <= self.store.clock())))).first() is not None

    def _snapshot(self, grant):
        with schema.read(self.store.engine) as db:
            if self._executing(db, grant):
                return None
            scope = (db, grant["tenant"], grant["employee_id"], grant["system"])
            active = self.store._active_grants(*scope)
            if not active:
                return None
            return {"signature": self._signature(active), "expected": self.store.desired_level(*scope)}

    def _mark(self, grant, snapshot, current, expected):
        with self.store.engine.begin() as db:
            # A grant may have completed while the external read was in flight.
            if self._executing(db, grant):
                return []
            active = self.store._active_grants(db, grant["tenant"], grant["employee_id"], grant["system"])
            if self._signature(active) != snapshot["signature"]:
                return []
            return self.store._drift_grants(db, active, current, expected, reason="巡检发现漂移")

    def _deliver(self, tenant):
        # Audit is the durable source; ingest's source cursor deduplicates delivery after a crash.
        with schema.read(self.store.engine) as db:
            notices = db.execute(select(audit, approvals.c.conv_id).join(approvals, approvals.c.id == audit.c.approval_id)
                .where(audit.c.tenant == tenant, audit.c.event == "grant_drift_detected",
                       approvals.c.conv_id.startswith("task:")).order_by(audit.c.id)).mappings().all()
        for notice in notices:
            self.tasks.ingest(notice["conv_id"][5:], source="grant_reconciler", object_id=notice["action_id"],
                version=notice["id"], kind="grant_drift", text="巡检发现权限与授权台账不一致，已停止自动回收，请人工核查。",
                data={"approval_id": notice["approval_id"], **notice["detail"]})
