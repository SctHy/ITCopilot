"""Read-only verification port. Observations never execute an approval or trust model prose."""
from typing import Protocol
import asyncio

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from core.db import directory_devices, directory_tickets, read
from core.tenant_context import SYSTEM, current_tenant, tenant_scope
from workflow.adapters.base import rank
from workflow.policy import load_policy
from workflow.tasks import TaskConflict


class GoalVerifier(Protocol):
    def normalize(self, checks: list[dict]) -> list[dict]: ...
    def catalog(self) -> list[dict]: ...
    async def verify(self, task: dict, checks: list[dict]) -> list[dict]: ...


class AccessGoalVerifier:
    """Verify the requester's current entitlement; this does not prove login/clone connectivity."""
    def __init__(self, registry=None, policy=load_policy):
        self.registry, self.policy = registry, policy

    def catalog(self):
        # Requestable systems of the tenant in scope (the room's, for goal edits and verification).
        tenant = current_tenant()
        policy = self.policy(tenant) if tenant and tenant != SYSTEM else self.policy()
        return [{'system': name, 'roles': list(rule['roles'])} for name, rule in policy.data['systems'].items()]

    def normalize(self, checks):
        if not isinstance(checks, list) or len(checks) > 10:
            raise TaskConflict('最多配置 10 条核验条件')
        systems = {r['system']: r['roles'] for r in self.catalog()}
        normalized = []
        for check in checks:
            if (not isinstance(check, dict) or set(check) != {'kind', 'system', 'role'}
                    or check['kind'] != 'access_level' or check['system'] not in systems
                    or check['role'] not in systems[check['system']]):
                raise TaskConflict('核验条件必须使用已配置的系统和角色；核验对象固定为申请人')
            if check not in normalized:
                normalized.append(dict(check))
        return normalized

    async def verify(self, task, checks):
        with tenant_scope(task['tenant']):  # adapters read the tenant's own directory (ADR 0023)
            return await self._verify(task, checks)

    async def _verify(self, task, checks):
        observations = []
        for check in checks:
            observation = {**check, 'status': 'unknown', 'actual': None}
            try:
                self.normalize([check])  # a policy change cannot silently retain obsolete criteria
                employee = task['principal'].get('employee_id')
                if not employee or self.registry is None:
                    raise ValueError('verifier unavailable')
                adapter = self.registry.for_action({'tool_name': 'request_access', 'arguments': {'system': check['system']}})
                actual = await adapter.current_level(employee, check['system'])
                expected = adapter.level(check['role'])
                observation.update(actual=actual, expected=expected, adapter=adapter.name,
                                   status='passed' if rank(actual) >= rank(expected) else 'failed')
            except Exception:
                # Do not disclose adapter exception strings, credentials, or target URLs.
                observation['reason'] = '无法读取或解释目标系统状态，需要人工核查'
            observations.append(observation)
        return observations


class DirectoryRecordVerifier:
    """Observe one of the requester's own directory records; ``judge`` maps the column value to an observation."""
    def __init__(self, engine, kind, key, column, judge, label):
        self.engine, self.kind, self.key, self.column, self.judge, self.label = engine, kind, key, column, judge, label

    def catalog(self):
        return []

    def normalize(self, checks):
        for check in checks:
            if set(check) != {"kind", self.key} or check["kind"] != self.kind or not isinstance(check[self.key], str) or not check[self.key]:
                raise TaskConflict(f"{self.label}核验需要 {self.kind} 和有效 {self.key}")
        return checks

    async def verify(self, task, checks):
        return await asyncio.to_thread(self._verify, task, checks)

    def _verify(self, task, checks):
        table = self.column.table
        observations = []
        with tenant_scope(task["tenant"]):
            for check in checks:
                try:
                    with read(self.engine) as db:
                        value = db.execute(select(self.column).where(table.c.tenant == task["tenant"],
                            table.c.employee_id == task["principal"]["employee_id"], table.c.id == check[self.key])).scalar()
                except OperationalError:
                    observations.append({**check, "status": "unknown", "actual": None, "reason": f"{self.label}台账暂时不可用"})
                    continue
                observations.append({**check, **self.judge(value)})
        return observations


def _device_locked(locked):
    # A device missing from the ledger is unknown, not unlocked.
    if locked is None:
        return {"status": "unknown", "actual": None, "expected": True}
    return {"status": "passed" if locked else "failed", "actual": bool(locked), "expected": True}


def _ticket_open(status):
    return {"status": "failed" if status is None or status == "closed" else "passed", "actual": status, "expected": "open"}


class GoalVerifiers:
    def __init__(self, registry, engine):
        self.access = AccessGoalVerifier(registry)
        self.verifiers = {
            "access_level": self.access,
            "device_locked": DirectoryRecordVerifier(engine, "device_locked", "device_id", directory_devices.c.locked, _device_locked, "设备"),
            "ticket_open": DirectoryRecordVerifier(engine, "ticket_open", "ticket_id", directory_tickets.c.status, _ticket_open, "工单"),
        }

    def catalog(self):
        return self.access.catalog()

    def normalize(self, checks):
        if not isinstance(checks, list) or len(checks) > 10:
            raise TaskConflict("最多配置 10 条核验条件")
        result = []
        for check in checks:
            if not isinstance(check, dict) or check.get("kind") not in self.verifiers:
                raise TaskConflict("未知的目标核验类型")
            normalized = self.verifiers[check["kind"]].normalize([check])[0]
            if normalized not in result:
                result.append(normalized)
        return result

    async def verify(self, task, checks):
        result = []
        for check in self.normalize(checks):
            result.extend(await self.verifiers[check["kind"]].verify(task, [check]))
        return result
