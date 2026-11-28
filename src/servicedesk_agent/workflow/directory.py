from __future__ import annotations

import time
from typing import Any, Dict, Iterable, Optional

from sqlalchemy import delete, func, insert, select, update

from servicedesk_agent.infra import db as schema
from servicedesk_agent.infra.db import (
    access_grants,
    directory_devices,
    directory_effects,
    directory_employees,
    directory_grants,
    directory_tickets,
)
from servicedesk_agent.infra.tenancy import DEFAULT_TENANT
from servicedesk_agent.infra.tenant_context import require_tenant, tenant_scope
from servicedesk_agent.workflow import devsuite
from servicedesk_agent.workflow.adapters.base import PermanentError
from servicedesk_agent.workflow.repository import SEED_EMPLOYEES, _iso


class DirectoryRepo:
    def __init__(self, database, clock=time.time):
        self.clock = clock
        self.database = database

    def _seed(self, tenant: str = DEFAULT_TENANT) -> None:
        """The demo sandbox directory; only the ``default`` tenant gets one at startup (ADR 0023)."""
        with tenant_scope(tenant), self.database.engine.begin() as db:
            devsuite.seed(db, tenant)
            employees = [{"tenant": tenant, "id": i, "username": u, "department": d} for i, u, d in SEED_EMPLOYEES]
            devices = [{"tenant": tenant, "id": f"DEV-{i}", "employee_id": i, "kind": "laptop", "locked": 0}
                       for i, _, _ in SEED_EMPLOYEES]
            for table, rows in ((directory_employees, employees), (directory_devices, devices)):
                db.execute(schema.upsert(db, table).on_conflict_do_nothing(), rows)

    def seed_directory(self, tenant: str) -> None:
        """Give a tenant the demo sandbox directory (evaluation, local multi-tenant trials)."""
        self._seed(tenant)

    def username_of(self, employee_id: str) -> Optional[str]:
        """Directory username in the current tenant scope (the worker runs each action in its tenant's)."""
        tenant = require_tenant()
        with schema.read(self.database.engine) as db:
            return db.execute(select(directory_employees.c.username).where(directory_employees.c.tenant == tenant,
                                                                           directory_employees.c.id == employee_id)).scalar()

    def employee(self, employee_id: str, tenant: str) -> Optional[Dict[str, Any]]:
        """Sandbox directory record and grant ledger of one employee of ``tenant``."""
        with schema.read(self.database.engine) as db:
            row = db.execute(select(directory_employees).where(directory_employees.c.tenant == tenant, directory_employees.c.id == employee_id)).mappings().first()
            if not row:
                return None
            grants = db.execute(select(directory_grants).where(directory_grants.c.tenant == tenant, directory_grants.c.employee_id == employee_id)
                                .order_by(directory_grants.c.system)).mappings().all()
            tickets = db.execute(select(directory_tickets.c.id, directory_tickets.c.title).where(directory_tickets.c.tenant == tenant,
                                                                                                directory_tickets.c.employee_id == employee_id)).all()
            devices = db.execute(select(directory_devices).where(directory_devices.c.tenant == tenant, directory_devices.c.employee_id == employee_id)
                                 .order_by(directory_devices.c.id)).mappings().all()
            ledger = db.execute(select(access_grants).where(access_grants.c.tenant == tenant, access_grants.c.employee_id == employee_id)
                                .order_by(access_grants.c.updated_at)).mappings().all()
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

    def reset_directory(self, employee_ids: Iterable[str], tenant: str = DEFAULT_TENANT) -> None:
        """Evaluation helper: restore seeded sandbox state between sequential cases."""
        ids = list(employee_ids)
        with tenant_scope(tenant), self.database.engine.begin() as db:
            db.execute(delete(directory_effects).where(directory_effects.c.tenant == tenant,
                directory_effects.c.result["action"].as_string().in_(devsuite.STEPS),
                directory_effects.c.result["employee_id"].as_string().in_(ids)))
            for table in (devsuite.groups, devsuite.licenses):
                db.execute(delete(table).where(devsuite.key(table, tenant), table.c.employee_id.in_(ids)))
            count = db.scalar(select(func.count()).select_from(devsuite.licenses).where(devsuite.key(devsuite.licenses, tenant),
                devsuite.licenses.c.active == 1))
            db.execute(update(devsuite.inventory).where(devsuite.key(devsuite.inventory, tenant)).values(
                total_seats=max(5, count), available_seats=max(5 - count, 0)))
            db.execute(update(devsuite.health).where(devsuite.key(devsuite.health, tenant)).values(status="healthy", message="正常"))
            db.execute(delete(directory_grants).where(directory_grants.c.tenant == tenant, directory_grants.c.employee_id.in_(ids)))
            db.execute(delete(directory_tickets).where(directory_tickets.c.tenant == tenant, directory_tickets.c.employee_id.in_(ids)))
            db.execute(update(directory_employees).where(directory_employees.c.tenant == tenant, directory_employees.c.id.in_(ids))
                       .values(locked=1, password_version=0, active=1))
            db.execute(update(directory_devices).where(directory_devices.c.tenant == tenant, directory_devices.c.employee_id.in_(ids))
                       .values(locked=0, operation_id=None))

    def devsuite_diagnosis(self, tool, employee, tenant):
        with schema.read(self.database.engine) as db:
            if tool == "check_employee_status":
                return devsuite.employee_status(db, tenant, employee)
            if tool == "check_software_access":
                return devsuite.access(db, tenant, employee, self.clock())
            table = devsuite.inventory if tool == "check_license_inventory" else devsuite.health
            result = dict(db.execute(select(table).where(devsuite.key(table, tenant))).mappings().one())
            if table is devsuite.inventory:
                result["available_seats"] += db.scalar(select(func.count()).select_from(devsuite.licenses)
                    .where(*devsuite.expired_seats(tenant, self.clock())))
            return result

    def preflight(self, action):
        tenant = require_tenant()
        with self.database.engine.connect() as db:
            row = db.execute(select(directory_employees.c.active).where(directory_employees.c.tenant == tenant,
                                                                        directory_employees.c.id == action["arguments"]["employee_id"])).first()
            device = None
            if action["tool_name"] == "lock_device":
                device = db.execute(select(directory_devices.c.employee_id).where(directory_devices.c.tenant == tenant,
                                                                                  directory_devices.c.id == action["arguments"]["device_id"])).first()
        if not row or (not row.active and action["tool_name"] != "create_ticket"):
            raise PermanentError("员工不存在或已停用")
        if action["tool_name"] == "lock_device" and (device is None or device.employee_id != action["arguments"]["employee_id"]):
            # The approval names both; locking someone else's device must fail before any effect.
            raise PermanentError("设备不存在或不属于该员工")

    def current_level(self, employee_id, system):
        tenant = require_tenant()
        with self.database.engine.connect() as db:
            return db.execute(select(directory_grants.c.role).where(
                directory_grants.c.tenant == tenant, directory_grants.c.employee_id == employee_id, directory_grants.c.system == system)).scalar()

    def observe(self, action_id, action=None):
        tenant = require_tenant()
        with self.database.engine.connect() as db:
            if action and action["tool_name"] in devsuite.STEPS:
                return devsuite.observe(db, tenant, action_id, action, self.clock())
            return db.execute(select(directory_effects.c.operation_id).where(directory_effects.c.tenant == tenant,
                                                                             directory_effects.c.operation_id == action_id)).first() is not None

    def apply(self, action_id, action):
        name, args = action["tool_name"], action["arguments"]
        employee, tenant = args["employee_id"], require_tenant()
        D, G, T, V = directory_employees, directory_grants, directory_tickets, directory_devices
        with self.database.engine.begin() as db:
            if name in devsuite.STEPS:
                return devsuite.apply(db, tenant, action_id, action, self.clock)
            done = db.execute(select(directory_effects.c.result).where(directory_effects.c.tenant == tenant,
                                                                       directory_effects.c.operation_id == action_id)).first()
            if done:
                return done.result
            account = db.execute(select(D.c.active).where(D.c.tenant == tenant, D.c.id == employee)).first()
            if not account or (not account.active and name != "create_ticket"):
                raise PermanentError("员工不存在或已停用")
            result: Dict[str, Any] = {"action": name, "employee_id": employee}
            grant_key = (G.c.tenant == tenant) & (G.c.employee_id == employee) & (G.c.system == args.get("system"))
            if name == "request_access":
                db.execute(delete(directory_grants).where(grant_key))
                db.execute(insert(directory_grants).values(tenant=tenant, employee_id=employee, system=args["system"], role=args["role"], operation_id=action_id))
                result.update(system=args["system"], role=args["role"])
            elif name == "revoke_access":
                db.execute(delete(directory_grants).where(grant_key))
                if args["restore_to"] is not None:
                    db.execute(insert(directory_grants).values(tenant=tenant, employee_id=employee, system=args["system"], role=args["restore_to"], operation_id=action_id))
                result.update(system=args["system"], restored=args["restore_to"])
            elif name == "reset_password":
                db.execute(update(D).where(D.c.tenant == tenant, D.c.id == employee).values(password_version=D.c.password_version + 1))
            elif name == "unlock_account":
                db.execute(update(D).where(D.c.tenant == tenant, D.c.id == employee).values(locked=0))
            elif name == "lock_device":
                locked = db.execute(update(V).where(V.c.tenant == tenant, V.c.id == args["device_id"], V.c.employee_id == employee)
                                    .values(locked=1, operation_id=action_id)).rowcount
                if not locked:
                    raise PermanentError("设备不存在或不属于该员工")
                result["device_id"] = args["device_id"]
            elif name == "create_ticket":
                ticket = "INC-" + action_id[:12]
                db.execute(insert(T).values(tenant=tenant, id=ticket, employee_id=employee, title=args["title"], description=args["description"], status="open"))
                result["ticket_id"] = ticket
            else:
                raise PermanentError(f"本地适配器不支持 {name}")
            db.execute(insert(directory_effects).values(tenant=tenant, operation_id=action_id, result=result))
        return result
