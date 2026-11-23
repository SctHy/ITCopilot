"""IT 服务台多轮对话状态：跨轮累积员工、设备、身份、网络和工单实体，并据此计算仍缺失的必填字段。"""
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List
from servicedesk_agent.agent.states import SLOTS, slot_state

ENTITY_KEYS = ("employee_id", "device_id", "ticket_id", "system", "role", "error_code", "department")
# 意图组级别的必填实体；意图级的写操作槽位见 servicedesk_agent.agent.intent.recognizer.REQUIRED_SLOTS。
GROUP_REQUIREMENTS = {"access": ["employee_id"], "network": ["error_code"], "endpoint": ["device_id"], "ticket": ["ticket_id"]}


TOOL_SLOTS = {
    "query_ticket": {"ticket_id"},
    "lock_device": {"employee_id", "device_id", "reason"},
    "unlock_account": {"employee_id"},
    "reset_password": {"employee_id"},
    "request_access": {"employee_id", "system", "role", "reason"},
    # Ticket creation also requires an identified employee at intent validation.
    "create_ticket": {"title", "description", "employee_id"},
}
SLOT_ALIASES = {"asset_id": "device_id"}
KNOWN_SLOTS = set.union(*TOOL_SLOTS.values())


def blocking_slots(name, missing):
    required = TOOL_SLOTS.get(name)
    return [slot for slot in missing if required is None or
            (canonical := SLOT_ALIASES.get(slot, slot)) in required or canonical not in KNOWN_SLOTS]


def merge_state(previous: Dict[str, Any], intent_group: str, entities: Dict[str, Any], message: str, missing_slots: Iterable[str] = (), *, intent=None) -> Dict[str, Any]:
    """合并本轮识别结果。missing_slots（意图识别给出的本轮缺失槽位）中已被历史轮次提供的实体会被剔除。"""
    state = dict(previous or {})
    known = dict(state.get("entities") or {})
    for key in ENTITY_KEYS:
        if entities.get(key):
            known[key] = entities[key]
    state.update({
        "schema_version": 3, "domain": "it_helpdesk", "intent_group": intent_group, "entities": known,
        "updated_at": datetime.now(timezone.utc).isoformat(), "turn_count": int(state.get("turn_count", 0)) + 1,
    })
    if intent is not None:
        from servicedesk_agent.agent.intent.taxonomy import REQUIRED_SLOTS, IntentCategory
        required = REQUIRED_SLOTS.get(IntentCategory(intent), ())
    else:
        required = GROUP_REQUIREMENTS.get(intent_group, [])
    missing = [key for key in required if not known.get(key)]
    missing += [slot for slot in missing_slots if not known.get(slot) and slot not in missing]
    state["missing_fields"] = sorted(missing)
    state["phase"] = SLOTS.check(slot_state(previous), slot_state(state))
    return state


def knowledge_for_turn(hits, previous):
    from servicedesk_agent.agent.tools import slim_hit
    seen = set(previous)
    results = []
    for hit in hits:
        identity = [hit.get(k) for k in ("document_id", "chunk_id", "chunk", "updated_at", "title", "content")]
        fingerprint = hashlib.sha256(json.dumps(identity, default=str).encode()).hexdigest()
        item = slim_hit(hit)
        if fingerprint in seen:
            item = {**item, "content": "见前文同一引用。"}
        results.append(item)
        seen.add(fingerprint)
    return results, sorted(seen)


IDENTITY_SENSITIVE_INTENTS = {"password_reset", "account_unlock"}


def with_login_employee(entities: Dict[str, List[str]], previous: Dict[str, Any], employee_id, intent: str) -> Dict[str, List[str]]:
    """建单、权限申请默认为本人：登录身份的工号算作已知（策略只允许为自己申请，代办时消息里会写明工号）。

    否则用户没重复报工号时写操作会被缺槽拦下，Agent 只能再问一遍。密码重置、账号解锁是身份敏感操作，
    仍要用户明确给出工号。优先级：本轮消息 > 历史轮次 > 登录身份。
    """
    if not employee_id or intent in IDENTITY_SENSITIVE_INTENTS or entities.get("employee_id") or (previous.get("entities") or {}).get("employee_id"):
        return dict(entities)
    return {**entities, "employee_id": [employee_id]}


def known_entities(state: Dict[str, Any], current: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """传给 Agent 的实体：历史轮次已知实体 + 本轮新识别实体（本轮优先）。"""
    return {**(state.get("entities") or {}), **{k: v for k, v in current.items() if v}}
