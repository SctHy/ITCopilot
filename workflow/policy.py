"""Versioned access policy: what may be requested, by whom, and how many approvals it needs."""
from __future__ import annotations

import logging
import os
import pathlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, FrozenSet, Optional, Tuple

import yaml

_ROOT = pathlib.Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)

REQUIRED_ARGS = {
    "request_access": ("employee_id", "system", "role", "reason"),
    "reset_password": ("employee_id",),
    "unlock_account": ("employee_id",),
    "create_ticket": ("employee_id", "title", "description"),
    "lock_device": ("employee_id", "device_id", "reason"),
}


class PolicyViolation(ValueError):
    """The request is well-formed but not allowed by policy."""


@dataclass(frozen=True)
class ActionRequirement:
    approvals: int
    duration_hours: Optional[int] = None  # only for grants that expire


class AccessPolicy:
    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.version = str(data["version"])
        self.approval_ttl_hours = int(data.get("approval_ttl_hours", 72))
        self.on_behalf_roles = frozenset(data.get("on_behalf_roles", []))
        self.approver_roles = frozenset(data.get("approver_roles", []))
        self.role_aliases = {str(k).casefold(): v for k, v in (data.get("role_aliases") or {}).items()}

    def normalise(self, action: Dict[str, Any]) -> Dict[str, Any]:
        """Validate shape and return the canonical action that gets frozen into the approval."""
        name, args = action.get("tool_name"), action.get("arguments", {})
        if name not in REQUIRED_ARGS or not isinstance(args, dict):
            raise ValueError("不支持的审批操作")
        if any(not isinstance(args.get(k), str) or not args[k].strip() for k in REQUIRED_ARGS[name]):
            raise ValueError("审批参数缺失或为空")
        args = {k: (v.strip() if isinstance(v, str) else v) for k, v in args.items()}
        if name == "request_access":
            system = args["system"].casefold()
            role = self.role_aliases.get(args["role"].casefold(), args["role"].casefold())
            rule = (self.data.get("systems", {}).get(system) or {}).get("roles", {}).get(role)
            if rule is None:
                raise PolicyViolation(f"策略 {self.version} 不允许申请 {args['system']} 的 {args['role']} 角色；管理员权限不能通过自助申请获得")
            hours = args.get("duration_hours", rule["default_hours"])
            if not isinstance(hours, int) or isinstance(hours, bool) or hours < 1:
                raise ValueError("duration_hours 必须是正整数")
            if hours > rule["max_hours"]:
                raise PolicyViolation(f"{system}/{role} 最长可申请 {rule['max_hours']} 小时")
            args.update(system=system, role=role, duration_hours=hours)
        elif name == "lock_device":
            args["device_id"] = args["device_id"].upper()  # asset tags are matched exactly downstream
        return {"tool_name": name, "arguments": args}

    def requirement(self, action: Dict[str, Any]) -> ActionRequirement:
        name, args = action["tool_name"], action["arguments"]
        if name == "request_access":
            rule = self.data["systems"][args["system"]]["roles"][args["role"]]
            return ActionRequirement(int(rule["approvals"]), int(args["duration_hours"]))
        return ActionRequirement(int((self.data.get("actions", {}).get(name) or {}).get("approvals", 1)))

    def check_requester(self, action: Dict[str, Any], requester_employee_id: Optional[str], requester_roles) -> None:
        target = action["arguments"]["employee_id"]
        if target == requester_employee_id or self.on_behalf_roles.intersection(requester_roles):
            return
        raise PolicyViolation("只能为本人账号提交申请；代他人申请需由 IT 管理员发起")

    def still_allows(self, frozen_actions, requester: Optional[Tuple[Optional[str], FrozenSet[str]]] = None) -> Optional[str]:
        """Re-check actions frozen under an older version; returns why this version refuses them.

        ``requester`` is (employee_id, roles) as snapshotted when the approval was created, so a
        request filed on someone else's behalf is refused once ``on_behalf_roles`` no longer
        covers the role it was filed under. Approvals created before the snapshot existed pass
        ``None`` and keep the old behaviour (only the actions are re-checked)."""
        for action in frozen_actions:
            try:
                self.normalise(action)
                if requester is not None:
                    self.check_requester(action, *requester)
            except ValueError as exc:  # PolicyViolation included
                return f"策略 {self.version} 不再允许：{exc}"
        return None

    def max_hours(self, system: str, role: str) -> Optional[int]:
        """Longest grant this version allows for system/role, or None when the role is no longer requestable."""
        rule = (self.data.get("systems", {}).get(system) or {}).get("roles", {}).get(role)
        return int(rule["max_hours"]) if rule else None


@lru_cache(maxsize=4)
def _load(path: str, mtime_ns: int) -> AccessPolicy:
    return AccessPolicy(yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")))


_last_good: Dict[str, AccessPolicy] = {}


def load_policy() -> AccessPolicy:
    """The policy file as it is now: an edit takes effect on the next call, without a restart
    (the cache is keyed by mtime). Open approvals and grants are re-checked by ``WorkflowStore.revalidate``.
    A broken edit keeps the last version that loaded, so a typo cannot widen or stop access."""
    path = os.getenv("IT_COPILOT_ACCESS_POLICY", str(_ROOT / "config/access_policy.yaml"))
    try:
        policy = _load(path, pathlib.Path(path).stat().st_mtime_ns)
    except Exception:
        if path not in _last_good:
            raise
        logger.exception("access policy %s failed to load; keeping version %s", path, _last_good[path].version)
        return _last_good[path]
    _last_good[path] = policy
    return policy
