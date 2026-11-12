"""Durable settlement and fingerprints use the same stores as application requests."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from sqlalchemy import func, select

import api.main as api
from agents.agent_orchestrator import AgentOrchestrator
from agents.base_agent import BaseAgent
from agents.outcome import turn_outcome
from agents.types import AgentRunDeps, Request
from api import state
from core.change_log import record_deploy, record_change
from core.db import change_events, turn_records
from core.events import InMemoryEventBus
from core.retention import RetentionPolicy
from core.skill_loader import SkillManager
from core.tenant_context import tenant_scope
from core.turn_records import build_sha
from evaluation.fake_model import scripted_model
from tests.conftest import auth, with_read_tool
from tests.test_workflow import ALICE
from tooling.tool_spec import make_tool
from workflow.store import WorkflowStore


@pytest.fixture
def store(db_url):
    return WorkflowStore(db_url)


def test_chat_settles_body_free_record_with_complete_versions(store, monkeypatch):
    async def respond(messages, info):
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {
                "intent": "vpn_issue", "confidence": .9, "entities": {"error_code": ["VPN-691"]}})])
        if not any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            return ModelResponse(parts=[ToolCallPart("diagnose", {"code": "VPN-691"})])
        return ModelResponse(parts=[TextPart("请检查账号锁定状态。")])
    monkeypatch.setattr(state, "events", InMemoryEventBus())
    model = scripted_model(respond)
    orchestrator = with_read_tool(AgentOrchestrator("sk-local"))
    orchestrator.agent._llm = orchestrator._intent_recognizer.llm = model
    for name, value in {"orchestrator": orchestrator, "workflow": store, "memory": None, "kb": None,
                        "task_recall": None, "request_recall": None, "monitor": None, "events": InMemoryEventBus()}.items():
        monkeypatch.setattr(state, name, value)
    monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "true")
    http = TestClient(api.app, headers=auth("alice"))
    response = http.post("/chat", json={"message": "VPN报错VPN-691", "conv_id": "fake:channel"})
    assert response.status_code == 200, response.text
    with tenant_scope("default"), store.engine.connect() as db:
        row = db.execute(select(turn_records)).mappings().one()
    assert row["request_id"] == response.json()["request_id"]
    assert row["owner"] == ALICE.scope_key and row["tenant"] == "default" and row["channel"] == "web"
    assert row["outcome"] == "success" and row["intent_source"] == "llm_tool_use"
    assert row["model_requests"] == 2
    assert row["tools"] == [{"name": "diagnose", "outcome": "success", "error_class": "success", "latency_ms": row["tools"][0]["latency_ms"]}]
    assert all(len(row[k]) == 64 for k in ("prompt_fp", "tools_fp", "skill_fp"))
    assert row["agent_version"] == "build:test-build" and row["policy_version"] == store.policy.version
    assert "VPN-691" not in str(dict(row))
    assert row["latency_ms"] > 0 and row["first_token_ms"] is None
    streamed = http.post("/chat/stream", json={"message": "VPN报错VPN-691", "conv_id": "streamed"})
    assert "event: completed" in streamed.text
    with tenant_scope("default"), store.engine.connect() as db:
        streaming_row = db.execute(select(turn_records).where(turn_records.c.conv_id == "streamed")).mappings().one()
    assert streaming_row["first_token_ms"] > 0


def test_skill_reload_changes_runtime_fingerprint_and_records_event(store, tmp_path):
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("# Example\n旧规则", encoding="utf-8")
    skills = SkillManager(str(tmp_path))
    skills.load()
    skills.change_engine = store.engine
    agent = BaseAgent(None, "test", skills)
    before = agent.fingerprints()
    skill_file.write_text("# Example\n新规则", encoding="utf-8")
    skills.reload()
    after = agent.fingerprints()
    assert before["skill_fp"] != after["skill_fp"]
    assert before["prompt_fp"] == after["prompt_fp"]
    with store.engine.connect() as db:
        event = db.execute(select(change_events)).mappings().one()
    assert event["kind"] == "skills" and event["ref"] == after["skill_fp"]
    skills.reload()
    with store.engine.connect() as db:
        assert db.scalar(select(func.count()).select_from(change_events)) == 1


def test_schema_and_write_guard_failures_are_distinct():
    async def run():
        agent = BaseAgent(None, "test")
        req = Request("查询", allowed_writes=frozenset(), principal=ALICE)
        deps = AgentRunDeps(req)
        spec = make_tool("request_access", "申请", {"employee_id": {"type": "string"}}, lambda req, args: {}, required=["employee_id"], retry_safe=False)
        await agent._invoke_tool(deps, spec, {}, "invalid")
        await agent._invoke_tool(deps, spec, {"employee_id": "1001"}, "guard")
        assert [t["error_class"] for t in deps.traces] == ["input_invalid", "guard_blocked"]
    asyncio.run(run())


@pytest.mark.parametrize(("record", "expected"), [
    ({"status": "success"}, "success"),
    ({"status": "partial"}, "partial"),
    ({"status": "failed"}, "failed"),
    ({"status": "success", "signals": ["claim_noted"]}, "false_claim_noted"),
    ({"status": "failed", "error_code": "round_limit"}, "round_limit"),
    ({"status": "failed", "error_code": "model_unavailable"}, "model_unavailable"),
])
def test_outcome_has_one_definition(record, expected):
    assert turn_outcome(record) == expected


def test_build_revision_is_required(monkeypatch):
    monkeypatch.delenv("IT_COPILOT_BUILD_SHA")
    with pytest.raises(KeyError):
        build_sha()
    monkeypatch.setenv("IT_COPILOT_BUILD_SHA", " ")
    with pytest.raises(ValueError):
        build_sha()


def test_changes_api_filters_tenants_and_roles(store, monkeypatch):
    monkeypatch.setattr(state, "workflow", store)
    record_deploy(store.engine, "revision-a")
    record_deploy(store.engine, "revision-a")
    record_change(store.engine, "knowledge", "v1", {}, tenant="default")
    record_change(store.engine, "knowledge", "v2", {}, tenant="other")
    http = TestClient(api.app)
    assert http.get("/admin/changes", headers=auth("alice")).status_code == 403
    items = http.get("/admin/changes", headers=auth("erin")).json()["items"]
    assert {i["ref"] for i in items} == {"revision-a", "v1"}
    assert [i["ref"] for i in http.get("/admin/changes?kind=knowledge", headers=auth("erin")).json()["items"]] == ["v1"]


def test_turn_retention_setting_rejects_invalid_values(monkeypatch):
    monkeypatch.setenv("IT_COPILOT_RETENTION_TURN_DAYS", "not-a-number")
    with pytest.raises(ValueError):
        RetentionPolicy.from_env()
