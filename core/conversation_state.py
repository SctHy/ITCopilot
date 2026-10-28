"""IT 服务台多轮对话状态：跨轮累积员工、设备、身份、网络和工单实体，并据此计算仍缺失的必填字段。"""
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List

ENTITY_KEYS = ("employee_id", "device_id", "ticket_id", "system", "role", "error_code", "department")
# 意图组级别的必填实体；意图级的写操作槽位见 core.intent_recognizer.REQUIRED_SLOTS。
GROUP_REQUIREMENTS = {"access": ["employee_id"], "network": ["error_code"], "endpoint": ["device_id"], "ticket": ["ticket_id"]}


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
        from core.intent_recognizer import REQUIRED_SLOTS, IntentCategory
        required = REQUIRED_SLOTS.get(IntentCategory(intent), ())
    else:
        required = GROUP_REQUIREMENTS.get(intent_group, [])
    missing = [key for key in required if not known.get(key)]
    missing += [slot for slot in missing_slots if not known.get(slot) and slot not in missing]
    state["missing_fields"] = sorted(missing)
    return state


def known_entities(state: Dict[str, Any], current: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """传给 Agent 的实体：历史轮次已知实体 + 本轮新识别实体（本轮优先）。"""
    return {**(state.get("entities") or {}), **{k: v for k, v in current.items() if v}}
