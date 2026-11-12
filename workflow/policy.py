"""Versioned access policy: what may be requested, by whom, and how many approvals it needs."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import yaml

from workflow.actions import WRITE_ACTIONS

_ROOT = pathlib.Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)

REQUIRED_ARGS = {name: action.required_args for name, action in WRITE_ACTIONS.items()}


def fingerprint(data: Dict[str, Any]) -> str:
    """Short hash of the parsed rules (not the file text), stable across comment and formatting edits."""
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


class PolicyViolation(ValueError):
    """The request is well-formed but not allowed by policy."""


@dataclass(frozen=True)
class ActionRequirement:
    approvals: int
    duration_hours: Optional[int] = None  # only for grants that expire


class AccessPolicy:
    def __init__(self, data: Dict[str, Any], *, tenant: Optional[str] = None):
        self.data = data
        self.tenant = tenant          # None: the platform base (no overlay)
        # The identity of a policy is its declared label *and* its rules. Every approval, grant and
        # revalidation compares this string, so editing a rule without bumping ``version`` still
        # reads as a new policy and open work gets re-checked (comments and key order do not count).
        self.declared_version = str(data["version"])
        self.fingerprint = fingerprint(data)
        self.version = f"{self.declared_version}+{self.fingerprint}"
        self.approval_ttl_hours = int(data.get("approval_ttl_hours", 72))
        # Actions whose drafted request the requester must confirm before approvers see it, and how
        # long an unconfirmed draft lives (ADR 0020).
        self.requester_confirmation = frozenset(data.get("requester_confirmation", []))
        self.confirmation_ttl_hours = int(data.get("confirmation_ttl_hours", 24))
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

    def needs_confirmation(self, actions) -> bool:
        return any(a["tool_name"] in self.requester_confirmation for a in actions)

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


class PolicyOverlayError(ValueError):
    """A tenant overlay that would loosen the platform base, or that is malformed."""


OVERLAY_KEYS = {"version", "approval_ttl_hours", "confirmation_ttl_hours", "requester_confirmation",
                "on_behalf_roles", "approver_roles", "systems", "actions"}
ROLE_KEYS = {"approvals", "default_hours", "max_hours"}
TENANT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,79}")   # core.tenancy.TENANT_ID; never a path component otherwise


def _positive(value, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise PolicyOverlayError(f"{what} 必须是正整数")
    return value


def tighten(base: Dict[str, Any], overlay: Dict[str, Any], tenant: str) -> Dict[str, Any]:
    """The base policy narrowed by a tenant overlay (ADR 0025). An overlay can only make access harder:
    more approvals, shorter lifetimes, fewer requestable systems and roles, fewer people who may file on
    behalf of others or approve, more actions that need the requester's confirmation. Anything that
    would widen access - or that the base does not know - is refused, not ignored."""
    if not isinstance(overlay, dict):
        raise PolicyOverlayError("覆盖策略必须是 YAML 映射")
    unknown = set(overlay) - OVERLAY_KEYS
    if unknown:
        raise PolicyOverlayError(f"覆盖策略不支持的字段：{sorted(unknown)}")
    if not isinstance(overlay.get("version"), (str, int)) or not str(overlay["version"]).strip():
        raise PolicyOverlayError("覆盖策略必须声明 version")
    merged = json.loads(json.dumps(base))
    merged["version"] = f"{base['version']}/{tenant}:{overlay['version']}"
    for key, default in (("approval_ttl_hours", 72), ("confirmation_ttl_hours", 24)):
        if key in overlay:
            value, limit = _positive(overlay[key], key), int(base.get(key, default))
            if value > limit:
                raise PolicyOverlayError(f"{key} 不能超过平台基线 {limit}")
            merged[key] = value
    if "requester_confirmation" in overlay:
        extra = set(overlay["requester_confirmation"] or [])
        if not extra <= set(REQUIRED_ARGS):
            raise PolicyOverlayError(f"requester_confirmation 含未知操作：{sorted(extra - set(REQUIRED_ARGS))}")
        merged["requester_confirmation"] = sorted(set(base.get("requester_confirmation", [])) | extra)
    for key in ("on_behalf_roles", "approver_roles"):
        if key in overlay:
            narrowed = set(overlay[key] or [])
            if not narrowed <= set(base.get(key, [])):
                raise PolicyOverlayError(f"{key} 只能是平台基线的子集，不能加入 {sorted(narrowed - set(base.get(key, [])))}")
            merged[key] = sorted(narrowed)
    for system, system_rule in (overlay.get("systems") or {}).items():
        if system not in merged.get("systems", {}):
            raise PolicyOverlayError(f"平台基线没有系统 {system}，覆盖策略不能新增")
        if system_rule is None:
            del merged["systems"][system]                      # not requestable in this tenant
            continue
        unknown = set(system_rule) - {"roles"}
        if unknown or not isinstance(system_rule.get("roles", {}), dict):
            raise PolicyOverlayError(f"systems.{system} 只能包含 roles")
        roles = merged["systems"][system]["roles"]
        for role, rule in (system_rule.get("roles") or {}).items():
            if role not in roles:
                raise PolicyOverlayError(f"平台基线没有 {system}/{role}，覆盖策略不能新增")
            if rule is None:
                del roles[role]
                continue
            if not isinstance(rule, dict) or set(rule) - ROLE_KEYS:
                raise PolicyOverlayError(f"{system}/{role} 只能设置 {sorted(ROLE_KEYS)}")
            current = roles[role]
            approvals = _positive(rule.get("approvals", current["approvals"]), f"{system}/{role}.approvals")
            max_hours = _positive(rule.get("max_hours", current["max_hours"]), f"{system}/{role}.max_hours")
            if approvals < current["approvals"]:
                raise PolicyOverlayError(f"{system}/{role} 的审批人数不能少于平台基线 {current['approvals']}")
            if max_hours > current["max_hours"]:
                raise PolicyOverlayError(f"{system}/{role} 的最长时长不能超过平台基线 {current['max_hours']} 小时")
            default_hours = _positive(rule.get("default_hours", min(current["default_hours"], max_hours)), f"{system}/{role}.default_hours")
            if default_hours > max_hours:
                raise PolicyOverlayError(f"{system}/{role} 的默认时长不能超过最长时长")
            roles[role] = {"approvals": approvals, "default_hours": default_hours, "max_hours": max_hours}
    for name, rule in (overlay.get("actions") or {}).items():
        if name not in merged.get("actions", {}):
            raise PolicyOverlayError(f"平台基线没有操作 {name}，覆盖策略不能新增")
        if not isinstance(rule, dict) or set(rule) - {"approvals"}:
            raise PolicyOverlayError(f"actions.{name} 只能设置 approvals")
        floor = int((merged["actions"][name] or {}).get("approvals", 1))
        approvals = _positive(rule.get("approvals", floor), f"actions.{name}.approvals")
        if approvals < floor:
            raise PolicyOverlayError(f"{name} 的审批人数不能少于平台基线 {floor}")
        merged["actions"][name] = {**(merged["actions"][name] or {}), "approvals": approvals}
    return merged


@lru_cache(maxsize=4)
def _load(path: str, mtime_ns: int) -> AccessPolicy:
    return AccessPolicy(yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")))


@lru_cache(maxsize=256)
def _load_overlay(base_path: str, base_mtime_ns: int, overlay_path: str, overlay_mtime_ns: int, tenant: str) -> AccessPolicy:
    base = _load(base_path, base_mtime_ns)
    overlay = yaml.safe_load(pathlib.Path(overlay_path).read_text(encoding="utf-8"))
    return AccessPolicy(tighten(base.data, overlay, tenant), tenant=tenant)


_last_good: Dict[Tuple[str, Optional[str]], AccessPolicy] = {}


def base_path() -> str:
    return os.getenv("IT_COPILOT_ACCESS_POLICY", str(_ROOT / "config/access_policy.yaml"))


def overlay_dir() -> pathlib.Path:
    return pathlib.Path(os.getenv("IT_COPILOT_POLICY_DIR", str(_ROOT / "config/policies")))


def overlay_path(tenant: Optional[str]) -> Optional[pathlib.Path]:
    """The tenant's overlay file, if it has one. Only well-formed tenant ids name a file."""
    if not tenant or not TENANT_ID.fullmatch(tenant):
        return None
    path = overlay_dir() / f"{tenant}.yaml"
    return path if path.is_file() else None


def overlay_tenants() -> List[str]:
    directory = overlay_dir()
    return sorted(p.stem for p in directory.glob("*.yaml") if TENANT_ID.fullmatch(p.stem)) if directory.is_dir() else []


def load_policy(tenant: Optional[str] = None) -> AccessPolicy:
    """The policy for ``tenant`` as it is now: the platform base, narrowed by the tenant's overlay
    (``config/policies/<tenant>.yaml``) when there is one. An edit to either file takes effect on the
    next call, without a restart (caches are keyed by mtime); open work is re-checked by
    ``WorkflowStore.revalidate``. A broken or loosening edit keeps the last version that loaded, so a
    typo cannot widen or stop access; with no good version yet the call fails (fail closed)."""
    path = base_path()
    overlay = overlay_path(tenant)
    key = (path, tenant if overlay else None)
    try:
        if overlay is None:
            policy = _load(path, pathlib.Path(path).stat().st_mtime_ns)
        else:
            policy = _load_overlay(path, pathlib.Path(path).stat().st_mtime_ns, str(overlay), overlay.stat().st_mtime_ns, tenant)
    except Exception:
        if key not in _last_good:
            raise
        logger.exception("access policy %s failed to load; keeping version %s", overlay or path, _last_good[key].version)
        return _last_good[key]
    _last_good[key] = policy
    return policy


def policy_signature() -> str:
    """Changes whenever the base or any overlay's effective rules change: the worker's cue to re-check open work."""
    versions = [load_policy().version] + [load_policy(t).version for t in overlay_tenants()]
    return hashlib.sha256("|".join(versions).encode()).hexdigest()[:16]
