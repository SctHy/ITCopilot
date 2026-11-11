"""Sandbox directory adapter backed by the workflow database's ``directory_*`` tables.

Each effect and its operation id commit in one transaction, so ``observe`` is exact and
re-applying an action id returns the recorded result instead of repeating the effect.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine

from core.db import directory_devices, directory_effects, directory_employees, directory_grants, directory_tickets
from workflow.adapters.base import PermanentError


class LocalDirectoryAdapter:
    name = "local"

    def __init__(self, engine: Engine):
        self.engine = engine

    def level(self, role: str) -> str:
        return role

    async def preflight(self, action: Dict[str, Any]) -> None:
        await asyncio.to_thread(self._preflight, action)

    def _preflight(self, action):
        with self.engine.connect() as db:
            row = db.execute(select(directory_employees.c.active).where(directory_employees.c.id == action["arguments"]["employee_id"])).first()
            device = None
            if action["tool_name"] == "lock_device":
                device = db.execute(select(directory_devices.c.employee_id).where(directory_devices.c.id == action["arguments"]["device_id"])).first()
        if not row or not row.active:
            raise PermanentError("员工不存在或已停用")
        if action["tool_name"] == "lock_device" and (device is None or device.employee_id != action["arguments"]["employee_id"]):
            # The approval names both; locking someone else's device must fail before any effect.
            raise PermanentError("设备不存在或不属于该员工")

    async def current_level(self, employee_id: str, system: str) -> Optional[str]:
        return await asyncio.to_thread(self._current_level, employee_id, system)

    def _current_level(self, employee_id, system):
        with self.engine.connect() as db:
            return db.execute(select(directory_grants.c.role).where(
                directory_grants.c.employee_id == employee_id, directory_grants.c.system == system)).scalar()

    async def observe(self, action_id: str, action: Dict[str, Any]) -> bool:
        return await asyncio.to_thread(self._observe, action_id)

    def _observe(self, action_id):
        with self.engine.connect() as db:
            return db.execute(select(directory_effects.c.operation_id).where(directory_effects.c.operation_id == action_id)).first() is not None

    async def apply(self, action_id: str, action: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self._apply, action_id, action)

    def _apply(self, action_id, action):
        name, args = action["tool_name"], action["arguments"]
        employee = args["employee_id"]
        with self.engine.begin() as db:
            done = db.execute(select(directory_effects.c.result).where(directory_effects.c.operation_id == action_id)).first()
            if done:
                return done.result
            if not db.execute(select(directory_employees.c.id).where(directory_employees.c.id == employee, directory_employees.c.active == 1)).first():
                raise PermanentError("员工不存在或已停用")
            result: Dict[str, Any] = {"action": name, "employee_id": employee}
            grant_key = (directory_grants.c.employee_id == employee) & (directory_grants.c.system == args.get("system"))
            if name == "request_access":
                db.execute(delete(directory_grants).where(grant_key))
                db.execute(insert(directory_grants).values(employee_id=employee, system=args["system"], role=args["role"], operation_id=action_id))
                result.update(system=args["system"], role=args["role"])
            elif name == "revoke_access":
                db.execute(delete(directory_grants).where(grant_key))
                if args.get("baseline_level"):
                    db.execute(insert(directory_grants).values(employee_id=employee, system=args["system"], role=args["baseline_level"], operation_id=action_id))
                result.update(system=args["system"], restored=args.get("baseline_level"))
            elif name == "reset_password":
                db.execute(update(directory_employees).where(directory_employees.c.id == employee).values(password_version=directory_employees.c.password_version + 1))
            elif name == "unlock_account":
                db.execute(update(directory_employees).where(directory_employees.c.id == employee).values(locked=0))
            elif name == "lock_device":
                locked = db.execute(update(directory_devices).where(directory_devices.c.id == args["device_id"], directory_devices.c.employee_id == employee)
                                    .values(locked=1, operation_id=action_id)).rowcount
                if not locked:
                    raise PermanentError("设备不存在或不属于该员工")
                result["device_id"] = args["device_id"]
            elif name == "create_ticket":
                ticket = "INC-" + action_id[:12]
                db.execute(insert(directory_tickets).values(id=ticket, employee_id=employee, title=args["title"], description=args["description"]))
                result["ticket_id"] = ticket
            else:
                raise PermanentError(f"本地适配器不支持 {name}")
            db.execute(insert(directory_effects).values(operation_id=action_id, result=result))
        return result
