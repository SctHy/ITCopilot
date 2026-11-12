"""One catalog of write actions (workflow.actions); every other list is derived from it."""
import pytest

from agents.intent.taxonomy import WRITE_ACTIONS as INTENT_LABELS
from agents.tools import agent_tools
from workflow.actions import WRITE_ACTIONS, WRITE_TOOLS
from workflow.approval import UNSAFE_TOOLS, approval_summary
from workflow.policy import REQUIRED_ARGS, load_policy


def test_every_layer_sees_the_same_write_actions():
    tools = agent_tools()
    assert {name for name, spec in tools.items() if not spec.retry_safe} == WRITE_TOOLS
    assert set(UNSAFE_TOOLS) == set(REQUIRED_ARGS) == set(INTENT_LABELS) == WRITE_TOOLS
    # Every write action has a policy rule and a required employee_id target.
    policy = load_policy()
    assert set(policy.requester_confirmation) <= WRITE_TOOLS
    assert all("employee_id" in action.required_args for action in WRITE_ACTIONS.values())


def test_a_write_tool_missing_from_the_catalog_refuses_to_start(monkeypatch):
    import agents.tools as tools_module
    monkeypatch.setattr(tools_module, "WRITE_TOOLS", WRITE_TOOLS - {"lock_device"})
    with pytest.raises(RuntimeError, match="lock_device"):
        tools_module.agent_tools()


def test_risk_and_reversibility_come_from_the_catalog():
    assert approval_summary(["create_ticket"], "建单")["risk"] == "medium"
    assert approval_summary(["create_ticket", "lock_device"], "锁定")["risk"] == "high"
    assert [n for n, a in WRITE_ACTIONS.items() if a.reversible] == ["request_access"]
