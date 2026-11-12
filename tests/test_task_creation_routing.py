"""Creation resolves the actual intent and freezes the selected specification before execution."""
import asyncio
import json

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
import yaml

from agents.registry import AgentRegistry, default_spec
from core.agent_rollouts import AgentRollouts
from evaluation.application import application
from evaluation.fake_model import scripted_model
from tests.test_workflow import ALICE
from workflow.store import WorkflowStore
from workflow.tasks import TaskStore


def registry(directory, tools, routes):
    base = default_spec().model_dump(mode="json")
    (directory / "it.yaml").write_text(yaml.safe_dump(base))
    for version in ("1.0.0", "1.1.0"):
        (directory / f"network_{version}.yaml").write_text(yaml.safe_dump({**base, "name": "network_support",
            "version": version, "serves": {"intent_groups": ["network"]}}))
    return AgentRegistry(directory, tools=tools, routes=routes)


def release_gate(directory, spec):
    (directory / f"{spec.name}@{spec.version}.json").write_text(json.dumps({"spec": spec.name,
        "version": spec.version, "status": "passed", "fingerprint": spec.fingerprint}))


def test_http_creation_routes_actual_intent_reuses_cache_and_preserves_pinned_version(tmp_path):
    classifications = []

    async def reply(messages, info):
        if info.output_tools:
            classifications.append(1)
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {
                "intent": "vpn_issue", "confidence": .99, "entities": {"error_code": ["VPN-691"]}})])
        return ModelResponse(parts=[TextPart("请检查账号状态与 VPN 客户端配置。")])

    async def run():
        async with application(model=scripted_model(reply)) as (http, state):
            await state.task_worker.stop()
            reg = registry(tmp_path, set(state.orchestrator.agent.get_tools()), set(state.orchestrator.router.config.routes))
            rolls = AgentRollouts(state.workflow.engine, reg, reports=tmp_path)
            state.agent_rollouts = state.orchestrator.rollouts = rolls
            state.agent_registry = reg
            release_gate(tmp_path, reg.get("network_support", "1.1.0"))
            rolls.change("network_support", "canary", "test", tenant="default", version="1.1.0", percent=100)
            payload = {"title": "VPN排障", "message": "VPN客户端报错VPN-691", "client_request_id": "first"}
            response = await http.post("/tasks", json=payload)
            assert response.status_code == 201, response.text
            first_id = response.json()["id"]
            claim = state.tasks.claim()
            context = state.tasks.context(claim)
            assert context[0]["id"] == first_id
            assert context[0]["_agent"] == {"agent_spec": "network_support", "agent_version": "1.1.0"}
            assert len(classifications) == 1
            rolls.change("network_support", "rollback", "test", tenant="default")
            req, slots, _ = await state.task_worker.handle._prepare(*context)
            assert slots["agent_version"] == "1.1.0" and req.intent_group == "network"
            assert len(classifications) == 1
            result = await state.orchestrator._execute(req)
            assert result.success and result.observability["agent_spec"] == "network_support"
            assert result.observability["agent_version"] == "1.1.0"
            replay = await http.post("/tasks", json=payload)
            assert replay.status_code == 201 and replay.json()["id"] == first_id
            initial = replay.json()["events"][0]["data"]
            assert (initial["agent_spec"], initial["agent_version"]) == ("network_support", "1.1.0")
            second = await http.post("/tasks", json={**payload, "client_request_id": "second"})
            assert second.status_code == 201, second.text
            initial = second.json()["events"][0]["data"]
            assert (initial["agent_spec"], initial["agent_version"]) == ("network_support", "1.0.0")
            assert len(classifications) == 1
    asyncio.run(run())


def test_standalone_task_create_accepts_explicit_group_and_freezes_version(db_url, tmp_path):
    workflow = WorkflowStore(db_url)
    reg = registry(tmp_path, set(), {"agent_read", "agent_write"})
    rolls = AgentRollouts(workflow.engine, reg, reports=tmp_path)
    tasks = TaskStore(workflow.engine)

    def choose(principal, intent_group):
        selected = {}
        rolls.choose(principal.tenant, intent_group, principal.scope_key, selected)
        return selected
    tasks.choose_agent_version = choose
    task = tasks.create(ALICE, "VPN排障", "VPN连不上", "one", intent_group="network")
    claim = tasks.claim()
    assert tasks.context(claim)[0]["_agent"] == {"agent_spec": "network_support", "agent_version": "1.0.0"}
    assert task["events"][0]["data"]["agent_spec"] == "network_support"
    default = TaskStore(workflow.engine)
    standalone = default.create(ALICE, "独立存储", "无模型任务", "standalone")
    assert standalone["events"][0]["data"]["agent_spec"] == default_spec().name
    workflow.close()
