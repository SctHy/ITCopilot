"""The write actions the Agent may propose: the one place that says what they are.

Everything else derives from this catalog instead of keeping its own list:

- ``agents.tools`` registers exactly these as non-retry-safe tools (checked at build time);
- ``workflow.approval`` turns their successful dry-runs into an approval, with ``risk``;
- ``workflow.policy`` validates ``required_args`` before freezing;
- ``agents.intent.taxonomy`` asks the intent model whether the user wants each ``label``;
- ``workflow.store`` uses ``reversible`` to decide whether a failed batch can be compensated.

Adding a write tool therefore means adding one entry here plus its handler and policy rule.
Order matters only for the intent questions (stable request shape for the Jev cache).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Tuple


@dataclass(frozen=True)
class WriteAction:
    name: str
    label: str                       # what the user asks for, in the user's words (intent questions)
    required_args: Tuple[str, ...]   # non-empty strings the frozen request must carry
    risk: str                        # "high" | "medium": shown to approvers
    reversible: bool                 # a saga can undo it after a later step fails


WRITE_ACTIONS: Dict[str, WriteAction] = {a.name: a for a in (
    WriteAction("create_ticket", "建工单或提交工单", ("employee_id", "title", "description"), "medium", False),
    WriteAction("request_access", "申请或开通系统权限", ("employee_id", "system", "role", "reason"), "high", True),
    WriteAction("reset_password", "重置密码", ("employee_id",), "high", False),
    WriteAction("unlock_account", "解锁账号", ("employee_id",), "high", False),
    WriteAction("lock_device", "远程锁定丢失或被盗的设备", ("employee_id", "device_id", "reason"), "high", False),
)}

WRITE_TOOLS: FrozenSet[str] = frozenset(WRITE_ACTIONS)


def risk_of(names) -> str:
    return "high" if any(WRITE_ACTIONS[n].risk == "high" for n in names if n in WRITE_ACTIONS) else "medium"
