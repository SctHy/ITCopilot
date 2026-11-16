"""Tasks record creation metadata; recovery executes the currently deployed specification."""
import asyncio

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
import yaml

from agents.registry import AgentRegistry, default_spec
from evaluation.application import application
from evaluation.fake_model import scripted_model
from tests.test_workflow import ALICE
from workflow.store import WorkflowStore
from workflow.tasks import TaskStore


def registry(directory, tools, routes, version="1.0.0"):
    base = default_spec().model_dump(mode="json")
    (directory / "it.yaml").write_text(yaml.safe_dump(base))
    (directory / "network.yaml").write_text(yaml.safe_dump({**base, "name": "network_support",
        "version": version, "serves": {"intent_groups": ["network"]}}))
    return AgentRegistry(directory, tools=tools, routes=routes)


def test_task_recovery_uses_deployed_version_and_preserves_creation_history(tmp_path):
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
            tools, routes = set(state.orchestrator.agent.get_tools()), set(state.orchestrator.router.config.routes)
            reg = registry(tmp_path, tools, routes, version="1.1.0")
            state.agent_registry = state.orchestrator.registry = reg
            payload = {"title": "VPN排障", "message": "VPN客户端报错VPN-691", "client_request_id": "first"}
            response = await http.post("/tasks", json=payload)
            assert response.status_code == 201, response.text
            first_id = response.json()["id"]
            claim = state.tasks.claim()
            context = state.tasks.context(claim)
            assert context[0]["id"] == first_id
            assert context[0]["_agent"] == {"agent_spec": "network_support", "agent_version": "1.1.0"}
            assert len(classifications) == 1
            state.agent_registry = state.orchestrator.registry = registry(tmp_path, tools, routes)
            req, _ = await state.task_worker.handle._prepare(*context)
            assert req.conversation_state["agent_version"] == "1.1.0" and req.intent_group == "network"
            assert len(classifications) == 1
            result = await state.orchestrator._execute(req)
            # The creation record keeps 1.1.0 as history; execution uses the version selected by deployment.
            assert result.success and result.observability["agent_spec"] == "network_support"
            assert result.observability["agent_version"] == "1.0.0"
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


def test_task_store_accepts_runtime_metadata_without_requiring_agent_config(db_url, tmp_path, monkeypatch):
    workflow = WorkflowStore(db_url)
    reg = registry(tmp_path, set(), {"agent_read", "agent_write"})
    tasks = TaskStore(workflow.engine)

    def choose(principal, intent_group):
        spec = reg.choose(principal.tenant, intent_group)
        return {"agent_spec": spec.name, "agent_version": spec.version}
    tasks.choose_agent_version = choose
    task = tasks.create(ALICE, "VPN排障", "VPN连不上", "one", intent_group="network")
    claim = tasks.claim()
    assert tasks.context(claim)[0]["_agent"] == {"agent_spec": "network_support", "agent_version": "1.0.0"}
    assert task["events"][0]["data"]["agent_spec"] == "network_support"
    def unavailable():
        raise AssertionError("TaskStore must not load Agent configuration")
    monkeypatch.setattr("agents.registry.default_spec", unavailable)
    default = TaskStore(workflow.engine)
    standalone = default.create(ALICE, "独立存储", "无模型任务", "standalone")
    assert "agent_spec" not in standalone["events"][0]["data"]
    assert default.context(default.claim())[0]["_agent"] == {}
    workflow.close()
