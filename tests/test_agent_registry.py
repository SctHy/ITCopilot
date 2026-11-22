from pathlib import Path

import pytest
import yaml

from it_copilot.agent.registry import AgentRegistry
from it_copilot.infra.tenant_context import system_scope

CONFIG = Path(__file__).resolve().parents[1] / "config/agents/it_copilot.yaml"

def registry(tmp_path, *, extra=()):
    value = yaml.safe_load(CONFIG.read_text())
    (tmp_path / "stable.yaml").write_text(yaml.safe_dump(value, allow_unicode=True))
    for i, patch in enumerate(extra):
        (tmp_path / f"extra{i}.yaml").write_text(yaml.safe_dump({**value, **patch}, allow_unicode=True))
    return AgentRegistry(tmp_path, tools={"read"}, routes={"agent_read", "agent_write"})



def test_registry_capability_specificity_and_ambiguity(tmp_path):
    loaded = registry(tmp_path, extra=[{"name": "security", "serves": {"intent_groups": ["security"]}}])
    assert loaded.resolve("default", "security") == "security"
    assert loaded.resolve("default", "network") == "it_copilot"
    assert loaded.get("it_copilot", "1.0.0").system_prompt == yaml.safe_load(CONFIG.read_text())["system_prompt"]
    assert loaded.choose("default", "security").name == "security"
    with pytest.raises(KeyError):
        loaded.get("it_copilot", "9.0.0")
    with pytest.raises(ValueError, match="Ambiguous"):
        registry(tmp_path, extra=[{"name": "other"}])



@pytest.mark.parametrize("patch, message", [
    ({"tools": ["missing"]}, "Unknown tools"),
    ({"routes": {"read": "missing", "write": "agent_write"}}, "Unknown Agent routes"),
    ({"skills": {"root": "missing", "disclosure": "full"}}, "Missing skill directory"),
])
def test_invalid_specs_fail_at_loading(tmp_path, patch, message):
    with pytest.raises(ValueError, match=message):
        registry(tmp_path, extra=[{"name": "invalid", **patch}])



def test_malformed_mcp_reference_is_rejected_at_loading(tmp_path):
    with pytest.raises(ValueError, match="Malformed"):
        registry(tmp_path, extra=[{"name": "invalid", "tools": ["mcp:broken"]}])



def test_default_spec_preserves_pre_registry_instructions_and_tools():
    from it_copilot.agent.base_agent import BaseAgent
    from it_copilot.agent.prompting import AGENT_RULES
    from it_copilot.agent.types import Request
    from it_copilot.agent.skill_loader import SkillManager
    from it_copilot.agent.tools import agent_tools
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
    from it_copilot.agent.base_agent import BaseAgent
    from it_copilot.agent.registry import default_spec
    spec = default_spec().model_copy(update={"tools": frozenset({"mcp:docs/search"})})
    agent = BaseAgent(None, "test", spec=spec)
    agent.set_shared_tools({})
    assert agent.get_tools() == {}



def test_registered_agent_uses_only_spec_skill_root(tmp_path, db_url):
    from it_copilot.agent.agent_orchestrator import AgentOrchestrator
    from it_copilot.agent.types import Request
    from it_copilot.agent.skill_loader import SkillManager
    from tests.test_workflow import ALICE
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("# Outside\nOUTSIDE_SPEC_SKILL")
    manager = SkillManager(str(outside))
    manager.load()
    reg = registry(tmp_path)
    orchestrator = AgentOrchestrator(skill_manager=manager)
    orchestrator.registry = reg
    req = Request("VPN", principal=ALICE, intent_group="network", conversation_state={})
    active = orchestrator.agent_for(req)
    assert "OUTSIDE_SPEC_SKILL" not in active._build_system_prompt(req)
    assert active._skill_manager.root_dir == (CONFIG.parents[2] / active.spec.skills.root).resolve()
    active.stats.total, active.stats.success = 1, 1
    assert orchestrator.get_stats()["total"] == 1



def test_confirmation_records_the_deployed_spec(tmp_path):
    import asyncio
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
    from tests.support.application import application
    from tests.support.fake_model import scripted_model
    from it_copilot.infra.db import turn_records
    from sqlalchemy import select
    reg = registry(tmp_path)
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
            state.orchestrator.registry = reg
            first = await http.post("/chat", json={"message": "工号1001申请GitLab只读24小时，排查问题", "conv_id": "sticky"})
            assert first.status_code == 200 and first.json()["awaiting_confirmation"], first.text
            second = await http.post("/chat", json={"message": "确认提交", "conv_id": "sticky"})
            assert second.status_code == 200, second.text
            with system_scope(), state.workflow.engine.connect() as db:
                records = list(db.execute(select(turn_records).order_by(turn_records.c.created_at)).mappings())
            assert len(records) == 2
            assert [row["agent_version"] for row in records] == ["1.0.0", "1.0.0"]
            assert records[1]["route"] == "confirmation" and records[1]["model"] == "none"
    asyncio.run(run())



def test_only_one_version_per_name_is_deployed(tmp_path):
    with pytest.raises(ValueError, match="one deployed version"):
        registry(tmp_path, extra=[{"version": "1.1.0"}])


def test_old_conversation_version_is_history_not_a_runtime_pin(tmp_path):
    from it_copilot.agent.agent_orchestrator import AgentOrchestrator
    from it_copilot.agent.types import Request
    from tests.test_workflow import ALICE
    orchestrator = AgentOrchestrator()
    orchestrator.registry = registry(tmp_path)
    state = {"agent_spec": "retired", "agent_version": "9.0.0"}
    req = Request("VPN", principal=ALICE, intent_group="network", conversation_state=state)
    assert orchestrator.agent_for(req).spec.version == "1.0.0"
    assert state == {"agent_spec": "it_copilot", "agent_version": "1.0.0"}


def test_admin_inspects_deployment_but_cannot_change_versions(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from tests.runtime import state
    from tests.runtime import app
    from tests.conftest import auth
    monkeypatch.setattr(state, "agent_registry", registry(tmp_path))
    client = TestClient(app)
    assert client.get("/admin/agents", headers=auth("alice")).status_code == 403
    response = client.get("/admin/agents", headers=auth("erin"))
    assert response.status_code == 200 and response.json()[0]["version"] == "1.0.0"
    for action in ("canary", "promote", "rollback"):
        assert client.post(f"/admin/agents/it_copilot/{action}", headers=auth("erin"), json={}).status_code == 404
