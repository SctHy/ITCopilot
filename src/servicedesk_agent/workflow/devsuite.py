"""DevSuite sandbox state. Directory effects and their receipts share a transaction."""
from __future__ import annotations

import hashlib

from sqlalchemy import delete, insert, select, update

from servicedesk_agent.infra import db as schema
from servicedesk_agent.infra.db import (
    actions, directory_access_groups as groups, directory_effects as effects,
    directory_employees as employees, directory_licenses as licenses,
    directory_service_health as health, directory_software_inventory as inventory,
)
from servicedesk_agent.workflow.adapters.base import AmbiguousError, PermanentError, ResourceExhausted
from servicedesk_agent.workflow.policy import PolicyViolation
from servicedesk_agent.workflow.repository import LeaseLost, canonical

SOFTWARE = "devsuite"
STEPS = {"assign_license", "add_access_group"}


def key(table, tenant):
    return (table.c.tenant == tenant) & (table.c.software == SOFTWARE)


def seed(db, tenant):
    for table, values in ((inventory, dict(total_seats=5, available_seats=5)),
                          (health, dict(env="prod", status="healthy", message="正常"))):
        db.execute(schema.upsert(db, table).values(tenant=tenant, software=SOFTWARE, **values).on_conflict_do_nothing())


def access(db, tenant, employee, now):
    license = db.execute(select(licenses).where(key(licenses, tenant), licenses.c.employee_id == employee)).mappings().first()
    role = license["scope"] if license else None
    expired = bool(license and license["expires_at"] <= now)
    active = bool(license and license["active"] and not expired)
    member = bool(role and db.scalar(select(groups.c.group_name).where(key(groups, tenant),
                  groups.c.employee_id == employee, groups.c.group_name == role)))
    return dict(software=SOFTWARE, env="prod", scope=role, expired=expired, active=active, group_member=member,
                has_access=active and member, expires_at=license["expires_at"] if license else None)


def employee_status(db, tenant, employee, *, lock=False):
    query = select(employees.c.active, employees.c.locked, employees.c.department).where(
        employees.c.tenant == tenant, employees.c.id == employee)
    row = db.execute(query.with_for_update() if lock else query).mappings().first()
    return dict(row) if row else dict(active=False, locked=True, department=None)


def repair_check(account, state, role):
    if not account["active"] or account["locked"]:
        raise PolicyViolation("account_unavailable：请先恢复账户")
    if not state["active"] or state["scope"] != role:
        raise PolicyViolation("license_scope_denied：请申请有效且范围匹配的许可证")
    if state["group_member"]:
        raise PolicyViolation("already_member：已具备访问权限")


def digest(action):
    return hashlib.sha256(canonical([action["tool_name"], action["arguments"]]).encode()).hexdigest()


def receipt(db, tenant, operation, action):
    result = db.scalar(select(effects.c.result).where(effects.c.tenant == tenant, effects.c.operation_id == operation))
    if result and result.get("digest") != digest(action):
        raise PermanentError("operation_conflict：幂等键对应的参数不一致")
    return result


def observe(db, tenant, operation, action, now):
    if not receipt(db, tenant, operation, action):
        return False
    args = action["arguments"]
    state = access(db, tenant, args["employee_id"], now)
    if not state["active"] or state["scope"] != args["role"] or (
            action["tool_name"] == "add_access_group" and not state["group_member"]):
        raise AmbiguousError("已找到操作回执，但当前许可证或访问组与回执不符")
    return True


def expired_seats(tenant, now):
    return (key(licenses, tenant), licenses.c.active == 1, licenses.c.expires_at <= now)


def assign_license(db, tenant, operation, args, state, now):
    employee, role = args["employee_id"], args["role"]
    if state["active"]:
        if state["scope"] != role:
            raise PermanentError("已有不同范围许可证，请人工核对")
        return
    # Serialize reclamation and allocation on this software's inventory row.
    db.execute(select(inventory.c.available_seats).where(key(inventory, tenant)).with_for_update())
    released = db.execute(update(licenses).where(*expired_seats(tenant, now)).values(active=0)).rowcount
    if released:
        db.execute(update(inventory).where(key(inventory, tenant)).values(available_seats=inventory.c.available_seats + released))
    reserved = db.execute(update(inventory).where(key(inventory, tenant), inventory.c.available_seats > 0)
                          .values(available_seats=inventory.c.available_seats - 1)).rowcount
    if not reserved:
        raise ResourceExhausted("insufficient_license_seats")
    db.execute(delete(groups).where(key(groups, tenant), groups.c.employee_id == employee))
    db.execute(delete(licenses).where(key(licenses, tenant), licenses.c.employee_id == employee))
    db.execute(insert(licenses).values(tenant=tenant, software=SOFTWARE, employee_id=employee, scope=role, active=1,
                                       operation_id=operation, expires_at=now + args["duration_hours"] * 3600))


def add_access_group(db, tenant, operation, args, state):
    if not state["active"] or state["scope"] != args["role"]:
        raise PermanentError("license_scope_denied：请重新申请许可证")
    db.execute(schema.upsert(db, groups).values(tenant=tenant, employee_id=args["employee_id"], software=SOFTWARE,
               group_name=args["role"], operation_id=operation).on_conflict_do_nothing())


def apply(db, tenant, operation, action, clock):
    args, name = action["arguments"], action["tool_name"]
    row = db.execute(select(actions).where(actions.c.tenant == tenant, actions.c.id == operation).with_for_update()).mappings().first()
    done = receipt(db, tenant, operation, action)
    if done:
        return done
    if (not row or row["status"] != "executing" or (row["lease_owner"], row["version"]) != action.get("lease")
            or row["lease_until"] < clock()):
        raise LeaseLost(operation)
    if row["arguments"] != args or row["tool_name"] != name:
        raise PermanentError("operation_conflict：动作与冻结参数不一致")
    employee = args["employee_id"]
    account = employee_status(db, tenant, employee, lock=True)
    if not account["active"] or account["locked"]:
        raise PermanentError("account_unavailable：员工账户未启用或已锁定")
    now = clock()
    state = access(db, tenant, employee, now)
    if name == "assign_license":
        assign_license(db, tenant, operation, args, state, now)
    else:
        add_access_group(db, tenant, operation, args, state)
    result = dict(action=name, employee_id=employee, scope=args["role"], digest=digest(action))
    db.execute(insert(effects).values(tenant=tenant, operation_id=operation, result=result))
    return result
