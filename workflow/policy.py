"""Versioned access policy: what may be requested, by whom, and how many approvals it needs."""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Optional

import yaml

_ROOT = pathlib.Path(__file__).resolve().parent.parent

REQUIRED_ARGS = {
    "request_access": ("employee_id", "system", "role", "reason"),
    "reset_password": ("employee_id",),
    "unlock_account": ("employee_id",),
    "create_ticket": ("employee_id", "title", "description"),
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


@lru_cache(maxsize=4)
def _load(path: str) -> AccessPolicy:
    return AccessPolicy(yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")))


def load_policy() -> AccessPolicy:
    return _load(os.getenv("IT_COPILOT_ACCESS_POLICY", str(_ROOT / "config/access_policy.yaml")))
