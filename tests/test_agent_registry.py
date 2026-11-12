import json
from pathlib import Path

import pytest
import yaml

from agents.registry import AgentRegistry
from core.agent_rollouts import AgentRollouts, canary_bucket
from core.db import shared_engine
from core.tenant_context import system_scope

CONFIG = Path(__file__).resolve().parents[1] / "config/agents/it_copilot.yaml"


def registry(tmp_path, *, extra=()):
    value = yaml.safe_load(CONFIG.read_text())
    (tmp_path / "stable.yaml").write_text(yaml.safe_dump(value, allow_unicode=True))
    for i, patch in enumerate(extra):
        (tmp_path / f"extra{i}.yaml").write_text(yaml.safe_dump({**value, **patch}, allow_unicode=True))
    return AgentRegistry(tmp_path, tools={"read"}, routes={"agent_read", "agent_write"})


def gate(path, spec):
    (path / f"{spec.name}@{spec.version}.json").write_text(json.dumps({
        "spec": spec.name, "version": spec.version, "status": "passed", "fingerprint": spec.fingerprint}))


def test_registry_capability_specificity_and_ambiguity(tmp_path):
    loaded = registry(tmp_path, extra=[{"name": "security", "serves": {"intent_groups": ["security"]}}])
    assert loaded.resolve("default", "security") == "security"
    assert loaded.resolve("default", "network") == "it_copilot"
    assert loaded.get("it_copilot", "1.0.0").system_prompt == yaml.safe_load(CONFIG.read_text())["system_prompt"]
    with pytest.raises(ValueError, match="Ambiguous"):
        registry(tmp_path, extra=[{"name": "other"}])


@pytest.mark.parametrize("patch", [
    {"tools": ["missing"]}, {"routes": {"read": "missing", "write": "agent_write"}},
    {"skills": {"root": "missing", "disclosure": "full"}},
])
def test_invalid_specs_fail_at_loading(tmp_path, patch):
    with pytest.raises(ValueError):
        registry(tmp_path, extra=[{"version": "1.1.0", **patch}])


def test_unreleased_version_cannot_expand_a_stable_specs_scope(tmp_path):
    with pytest.raises(ValueError, match="scope must agree"):
        registry(tmp_path, extra=[{"version": "1.1.0", "tenants": ["acme"]}])


def test_malformed_mcp_reference_is_rejected_at_loading(tmp_path):
    with pytest.raises(ValueError, match="Malformed"):
        registry(tmp_path, extra=[{"version": "1.1.0", "tools": ["mcp:broken"]}])


def test_buckets_are_stable_and_cover_expected_proportion():
    buckets = [canary_bucket("it_copilot", f"default:user-{i}") for i in range(10000)]
    assert buckets == [canary_bucket("it_copilot", f"default:user-{i}") for i in range(10000)]
    assert .18 < sum(bucket < 20 for bucket in buckets) / len(buckets) < .22


def test_canary_is_sticky_while_served_and_rollback_moves_pinned_conversations_back(tmp_path, db_url):
    reg = registry(tmp_path, extra=[{"version": "1.1.0"}])
    gate(tmp_path, reg.get("it_copilot", "1.1.0"))
    service = AgentRollouts(shared_engine(db_url), reg, cache_ttl_s=0, reports=tmp_path)
    service.change("it_copilot", "canary", "admin", tenant="default", version="1.1.0", percent=100, tenants=["default"])
    state = {}
    assert service.choose("default", "network", "default:user", state).version == "1.1.0"
    assert service.choose("other", "network", "other:user", {}).version == "1.0.0"
    service.change("it_copilot", "rollback", "admin", tenant="default")
    assert service.choose("default", "network", "default:user", state).version == "1.0.0"
    assert state["agent_version"] == "1.0.0"
    assert service.choose("default", "network", "default:user", {}).version == "1.0.0"
    with system_scope(), service.engine.connect() as db:
        from core.db import admin_audit, change_events
        from sqlalchemy import select
        assert len(db.execute(select(admin_audit)).all()) == 2
        assert len(db.execute(select(change_events)).all()) == 2
        assert set(db.execute(select(admin_audit.c.tenant)).scalars()) == {"*"}
        assert set(db.execute(select(change_events.c.tenant)).scalars()) == {None}


def test_pins_survive_a_shrinking_canary_and_follow_a_promotion(tmp_path, db_url):
    reg = registry(tmp_path, extra=[{"version": "1.1.0"}])
    gate(tmp_path, reg.get("it_copilot", "1.1.0"))
    service = AgentRollouts(shared_engine(db_url), reg, cache_ttl_s=0, reports=tmp_path)
    stable_pin = {}
    assert service.choose("default", "network", "default:a", stable_pin).version == "1.0.0"
    service.change("it_copilot", "canary", "admin", tenant="default", version="1.1.0", percent=100, tenants=None)
    canary_pin = {}
    assert service.choose("default", "network", "default:b", canary_pin).version == "1.1.0"
    assert service.choose("default", "network", "default:a", stable_pin).version == "1.0.0"
    service.change("it_copilot", "canary", "admin", tenant="default", version="1.1.0", percent=0, tenants=None)
    assert service.choose("default", "network", "default:b", canary_pin).version == "1.1.0"
    service.change("it_copilot", "promote", "admin", tenant="default")
    assert service.choose("default", "network", "default:a", stable_pin).version == "1.1.0"
    assert service.choose("default", "network", "default:b", canary_pin).version == "1.1.0"


def test_canary_without_release_report_is_rejected(tmp_path, db_url):
    reg = registry(tmp_path, extra=[{"version": "1.1.0"}])
    service = AgentRollouts(shared_engine(db_url), reg, reports=tmp_path)
    with pytest.raises(FileNotFoundError):
        service.change("it_copilot", "canary", "admin", tenant="default", version="1.1.0", percent=20)
    assert service.get("it_copilot")["status"] == "stable"


def test_persisted_version_references_are_checked_at_startup(tmp_path, db_url):
    from sqlalchemy import update
    from core.db import agent_rollouts
    reg = registry(tmp_path)
    engine = shared_engine(db_url)
    AgentRollouts(engine, reg)
    with system_scope(), engine.begin() as db:
        db.execute(update(agent_rollouts).values(stable_version="0.0.0"))
    with pytest.raises(KeyError):
        AgentRollouts(engine, reg)


def test_stale_automatic_rollback_cannot_roll_back_a_new_campaign(tmp_path, db_url):
    reg = registry(tmp_path, extra=[{"version": "1.1.0"}, {"version": "2.0.0"}])
    gate(tmp_path, reg.get("it_copilot", "1.1.0"))
    gate(tmp_path, reg.get("it_copilot", "2.0.0"))
    engine = shared_engine(db_url)
    a = AgentRollouts(engine, reg, reports=tmp_path)
    b = AgentRollouts(engine, reg, reports=tmp_path)
    a.change("it_copilot", "canary", "admin", tenant="default", version="1.1.0", percent=20)
    stale = a.get("it_copilot")
    b.change("it_copilot", "promote", "admin", tenant="default")
    b.change("it_copilot", "canary", "admin", tenant="default", version="2.0.0", percent=20)
    assert a.change("it_copilot", "rollback", "system", tenant=None, expected=stale) is None
    assert a.get("it_copilot")["canary_version"] == "2.0.0"
    assert a.get("it_copilot")["status"] == "canarying"


def test_synthetic_bad_canary_triggers_automatic_rollback(tmp_path, db_url):
    from sqlalchemy import insert
    from core.db import turn_records
    from tests.test_attribution import rows
    reg = registry(tmp_path, extra=[{"version": "1.1.0"}])
    gate(tmp_path, reg.get("it_copilot", "1.1.0"))
    service = AgentRollouts(shared_engine(db_url), reg, reports=tmp_path, clock=lambda: 5000)
    service.change("it_copilot", "canary", "admin", tenant="default", version="1.1.0", percent=20)
    before, after = rows(100, 0, start=4000), rows(100, 80, start=4200)
    with system_scope(), service.engine.begin() as db:
        for group, version in ((before, "1.0.0"), (after, "1.1.0")):
            for row in group:
                record = {**row, "agent_spec": "it_copilot", "agent_version": version,
                    "owner": "default:alice", "conv_id": "test", "outcome": row["status"],
                    "intent": "other", "intent_confidence": 1., "route": "agent_read", "build_sha": "test",
                    "output_tokens": 0, "latency_ms": 100., "stage_ms": {}}
                db.execute(insert(turn_records).values(**record))
    assert service.auto_rollback()[0]["reasons"] == ["failure_rate"]
    assert service.get("it_copilot")["status"] == "rolled_back"


def test_admin_api_requires_role_and_release_gate(tmp_path, db_url, monkeypatch):
    from fastapi.testclient import TestClient
    from api import state
    from api.main import app
    from tests.conftest import auth
    reg = registry(tmp_path, extra=[{"version": "1.1.0"}])
    service = AgentRollouts(shared_engine(db_url), reg, reports=tmp_path)
    monkeypatch.setattr(state, "agent_rollouts", service)
    client = TestClient(app)
    assert client.get("/admin/agents", headers=auth("alice")).status_code == 403
    assert client.get("/admin/agents", headers=auth("erin")).status_code == 200
    response = client.post("/admin/agents/it_copilot/canary", headers=auth("erin"),
                           json={"version": "1.1.0", "percent": 50})
    assert response.status_code == 409
    gate(tmp_path, reg.get("it_copilot", "1.1.0"))
    assert client.post("/admin/agents/it_copilot/canary", headers=auth("erin"),
                       json={"version": "1.1.0", "percent": 50}).status_code == 200
    assert client.post("/admin/agents/it_copilot/promote", headers=auth("erin")).json()["stable_version"] == "1.1.0"


def test_default_spec_preserves_pre_registry_instructions_and_tools():
    from agents.base_agent import BaseAgent
    from agents.prompting import AGENT_RULES
    from agents.types import Request
    from core.skill_loader import SkillManager
    from agents.tools import agent_tools
    manager = SkillManager(str(CONFIG.parents[2] / "skills"))
    manager.load()
    agent = BaseAgent(None, "test", manager)
    tools = agent_tools()
    agent.set_shared_tools(tools)
    original = "你是 IT-Copilot 的 IT 服务 Agent：设备、网络与软件、账号权限、工单。一条消息含多个问题时逐个处理。"
    original += "\n" + AGENT_RULES + "\n\n业务规范（与上面的规则冲突时以规则为准）：\n" + manager.prompt()
    assert agent._build_system_prompt(Request("测试")) == original
    assert set(agent.get_tools()) == set(tools)


def test_external_mcp_whitelist_tolerates_disconnection():
    from agents.base_agent import BaseAgent
    from agents.registry import default_spec
    spec = default_spec().model_copy(update={"tools": frozenset({"mcp:docs/search"})})
    agent = BaseAgent(None, "test", spec=spec)
    agent.set_shared_tools({})
    assert agent.get_tools() == {}


def test_registered_agent_uses_only_spec_skill_root(tmp_path, db_url):
    from agents.agent_orchestrator import AgentOrchestrator
    from agents.types import Request
    from core.skill_loader import SkillManager
    from tests.test_workflow import ALICE
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Outside\nOUTSIDE_SPEC_SKILL")
    manager = SkillManager(str(outside))
    manager.load()
    reg = registry(tmp_path)
    orchestrator = AgentOrchestrator(skill_manager=manager)
    orchestrator.rollouts = AgentRollouts(shared_engine(db_url), reg)
    req = Request("VPN", principal=ALICE, intent_group="network", conversation_state={})
    active = orchestrator.agent_for(req)
    assert "OUTSIDE_SPEC_SKILL" not in active._build_system_prompt(req)
    assert active._skill_manager.root_dir == (CONFIG.parents[2] / active.spec.skills.root).resolve()
    active.stats.total, active.stats.success = 1, 1
    assert orchestrator.get_stats()["total"] == 1


def test_canary_confirmation_records_the_sticky_spec(tmp_path):
    import asyncio
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from evaluation.application import application
    from evaluation.fake_model import scripted_model
    from core.db import turn_records
    from sqlalchemy import select
    reg = registry(tmp_path, extra=[{"version": "1.1.0"}])
    gate(tmp_path, reg.get("it_copilot", "1.1.0"))
    async def reply(messages, info):
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {"intent": "permission_request", "confidence": .99,
                "entities": {"employee_id": ["1001"], "system": ["GitLab"], "role": ["readonly"]}})])
        if not any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            return ModelResponse(parts=[ToolCallPart("request_access", {"employee_id": "1001", "system": "GitLab",
                "role": "readonly", "reason": "测试", "duration_hours": 24})])
        return ModelResponse(parts=[TextPart("已起草申请，等待确认。")])
    async def run():
        async with application(model=scripted_model(reply)) as (http, state):
            service = AgentRollouts(state.workflow.engine, reg, reports=tmp_path)
            service.change("it_copilot", "canary", "test", tenant="default", version="1.1.0", percent=100)
            state.orchestrator.rollouts = service
            first = await http.post("/chat", json={"message": "工号1001申请GitLab只读24小时，排查问题", "conv_id": "sticky"})
            assert first.status_code == 200 and first.json()["awaiting_confirmation"], first.text
            second = await http.post("/chat", json={"message": "确认提交", "conv_id": "sticky"})
            assert second.status_code == 200, second.text
            with system_scope(), state.workflow.engine.connect() as db:
                records = list(db.execute(select(turn_records).order_by(turn_records.c.created_at)).mappings())
            assert len(records) == 2
            assert [row["agent_version"] for row in records] == ["1.1.0", "1.1.0"]
            assert records[1]["route"] == "confirmation" and records[1]["model"] == "none"
    asyncio.run(run())
