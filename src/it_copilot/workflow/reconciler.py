"""Compare live entitlements with the ledger without changing the target system."""

from sqlalchemy import and_, or_, select

from it_copilot.infra import db as schema
from it_copilot.infra.db import access_grants, actions
from it_copilot.infra.tenant_context import system_scope, tenant_scope
from it_copilot.infra.monitor import metrics
from it_copilot.workflow.catalog import resource_key
from it_copilot.workflow.adapters.base import AmbiguousError, TransientError, target_level
from it_copilot.workflow.states import ActionStatus, GrantStatus


class GrantReconciler:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry

    async def run_once(self):
        with system_scope():
            tenants = await self.store.run(self._tenants)
        drifted = 0
        for tenant in tenants:
            with tenant_scope(tenant):
                cursor = ""
                while True:
                    batch = await self.store.run(self._batch, tenant, cursor)
                    if not batch:
                        break
                    for grant in batch:
                        cursor = grant["action_id"]
                        snapshot = await self.store.run(self._snapshot, grant)
                        if not snapshot:
                            continue
                        adapter = self.registry.by_name(grant["adapter"])
                        try:
                            current = await adapter.current_level(grant["employee_id"], grant["system"])
                        except (TransientError, AmbiguousError):
                            metrics.RECONCILE_SKIPPED.inc()
                            continue
                        expected = target_level(adapter, snapshot["expected"])
                        if current != expected:
                            changed = await self.store.run(self._mark, grant, snapshot, current, expected)
                            drifted += len(changed)
                            if changed:
                                metrics.GRANT_DRIFT.labels(system=grant["system"]).inc()
        return drifted

    def _tenants(self):
        with schema.read(self.store.engine) as db:
            return list(db.execute(select(access_grants.c.tenant).distinct().where(
                access_grants.c.status == GrantStatus.ACTIVE)).scalars())

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
