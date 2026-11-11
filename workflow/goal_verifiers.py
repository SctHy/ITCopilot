"""Read-only verification port. Observations never execute an approval or trust model prose."""
from typing import Protocol

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
        return [{'system': name, 'roles': list(rule['roles'])}
                for name, rule in self.policy().data['systems'].items()]

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
