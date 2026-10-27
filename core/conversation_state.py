"""IT 服务台对话状态：只保留员工、设备、身份、网络和工单实体。"""
import re
from datetime import datetime, timezone
from typing import Any, Dict

ENTITY_KEYS=("employee_id","device_id","ticket_id","system","role","error_code","department","urgency")
def merge_state(previous:Dict[str,Any], intent_group:str, entities:Dict[str,Any], message:str)->Dict[str,Any]:
    state=dict(previous or {})
    state["schema_version"] = 2
    state["domain"]="it_helpdesk"; state["intent_group"]=intent_group
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    state["turn_count"] = int(state.get("turn_count", 0)) + 1
    state["entities"] = dict(state.get("entities") or {})
    for key in ENTITY_KEYS:
        if entities.get(key):
            state[key]=entities[key]
            state["entities"][key]=entities[key]
    state["missing_fields"] = _missing_fields(intent_group, state["entities"])
    if re.search(r"转人工|人工|升级",message): state["needs_handoff"]=True
    return state

def _missing_fields(intent_group: str, entities: Dict[str, Any]):
    requirements={"access":["employee_id"],"network":["error_code"],"endpoint":["device_id"],"ticket":["ticket_id"]}
    return [field for field in requirements.get(intent_group, []) if not entities.get(field)]
