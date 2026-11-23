from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from servicedesk_agent.workflow.catalog import WRITE_ACTIONS

# Sandbox directory. Usernames double as Gitea accounts in the compose deployment.
SEED_EMPLOYEES = (
    ("1001", "alice", "engineering"), ("1002", "dave", "engineering"), ("2001", "bob", "engineering"),
    ("2002", "carol", "engineering"), ("3001", "fiona", "finance"), ("9001", "erin", "it"), ("9002", "grace", "security"),
    ("E1001", "e1001", "engineering"), ("EMP1001", "emp1001", "engineering"),
)


class ApprovalForbidden(PermissionError):
    """Caller lacks authority or would violate separation of duties."""


class ApprovalConflict(ValueError):
    """Frozen content mismatch (hash or reused approval id)."""


class ApprovalStateConflict(ValueError):
    """The approval or grant is no longer in a state that allows the requested change."""


class LeaseLost(RuntimeError):
    """Another worker reclaimed the action after this worker's lease expired."""


class _Retry(Exception):
    pass


def _reversible(tool_name: str) -> bool:
    action = WRITE_ACTIONS.get(tool_name)
    return bool(action and action.reversible)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


def _action_key(action: Dict[str, Any]) -> tuple:
    """What a request is about: same tool, same target employee and, for access, same system."""
    args = action["arguments"]
    return (action["tool_name"], args.get("employee_id"), args.get("system") if action["tool_name"] == "request_access" else None)


_ACTION_NAMES = {"request_access": "申请权限", "reset_password": "重置密码", "unlock_account": "解锁账号", "create_ticket": "创建工单",
                 "lock_device": "远程锁定设备"}


def _describe(action_requests: List[Dict[str, Any]]) -> str:
    parts = []
    for action in action_requests:
        args = action["arguments"]
        what = _ACTION_NAMES.get(action["tool_name"], action["tool_name"])
        if action["tool_name"] == "request_access":
            what += f" {args.get('system')} {args.get('role')}"
        elif action["tool_name"] == "create_ticket":
            what += f"「{str(args.get('title', ''))[:40]}」"
        elif action["tool_name"] == "lock_device":
            what += f" {args.get('device_id')}"
        parts.append(f"{what}（工号 {args.get('employee_id')}）")
    return "；".join(parts)


def action_id(approval_id: str, payload_hash: str, seq: int, kind: str = "apply") -> str:
    return hashlib.sha256(f"{approval_id}:{payload_hash}:{seq}:{kind}".encode()).hexdigest()



class Repository:
    def __init__(self, store):
        self.store = store
