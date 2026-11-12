"""Build body-free turn records at the authoritative settlement boundary."""
import os
import time

from sqlalchemy.dialects import postgresql, sqlite

from agents.outcome import turn_outcome
from core.db import turn_records


def build_sha():
    value = os.environ["IT_COPILOT_BUILD_SHA"].strip()
    if not value:
        raise ValueError("IT_COPILOT_BUILD_SHA must not be empty")
    return value


def build_record(req, result, *, channel, policy_version, kb_version, approval_id=None,
                 task_id=None, stage_ms=None, created_at=None):
    obs = result.observability
    usage = result.token_usage
    signals = list(obs["signals"])
    if result.error_code and result.error_code in {"round_limit", "bad_output", "model_unavailable", "deadline"}:
        signals.append(result.error_code)
    if result.escalated:
        signals.append("escalated")
    if req.intent_source in {"rule", "rules", "keyword", "fallback", "fallback_disabled", "fallback_error"}:
        signals.append("intent_rule_fallback")
    tools = [{"name": t["tool_name"], "outcome": "success" if t["success"] else "failed",
              "error_class": t["error_class"], "latency_ms": t["latency_ms"]} for t in result.tool_traces]
    if any(t["error_class"] not in {"success", "guard_blocked", "policy_denied"} for t in tools):
        signals.append("tool_error")
    record = dict(request_id=req.request_id, tenant=req.principal.tenant, owner=req.principal.scope_key,
        channel=channel, conv_id=req.conv_id, task_id=task_id, created_at=time.time() if created_at is None else created_at,
        status=result.status, error_code=result.error_code, intent=req.intent.value if req.intent else "other",
        intent_group=req.intent_group or "other", intent_source=req.intent_source, intent_confidence=req.intent_confidence,
        agent_spec=obs["agent_spec"], agent_version=obs["agent_version"], model=obs["model"], route=obs["route"],
        route_reason=obs["route_reason"], prompt_fp=obs["prompt_fp"], skill_fp=obs["skill_fp"], tools_fp=obs["tools_fp"],
        policy_version=policy_version, kb_version=str(kb_version), build_sha=obs["build_sha"],
        model_requests=usage.get("requests", 0), input_tokens=usage.get("input_tokens", 0),
        output_tokens=usage.get("output_tokens", 0), cache_read_tokens=usage.get("cache_read_tokens", 0),
        cost_usd=obs.get("cost_usd"), latency_ms=result.latency_ms, first_token_ms=obs["first_token_ms"],
        stage_ms={} if stage_ms is None else dict(stage_ms), tools=tools, signals=sorted(set(signals)), approval_id=approval_id)
    record["outcome"] = turn_outcome(record)
    return record


def record_turn(db, record):
    insert = (postgresql if db.dialect.name == "postgresql" else sqlite).insert(turn_records)
    db.execute(insert.values(**record).on_conflict_do_nothing(index_elements=[turn_records.c.request_id]))
