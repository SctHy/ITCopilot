"""Declared transition tables and the places that enforce them."""
import asyncio

import pytest

from agents.agent_stats import AgentHealth, AgentStats
from agents.planner import STEP, ExecutionPlan, PlanStep, StepStatus
from api import state
from api.routes import chat as chat_routes
from core.events import InMemoryEventBus
from core.states import IllegalTransition, Machine
from intake.im import INBOX, OUTBOX
from memory.conversation_memory import FACT, MAX_ACTIVE_FACTS, FactStatus, MemoryContext, MemoryManager
from monitor.performance_monitor import ALERT, AlertManager, Condition
from tooling.mcp_client import CONNECTION
from workflow.states import ACTION, APPROVAL, EXECUTION, GRANT, ActionStatus, ApprovalStatus, Execution, GrantStatus
from workflow.store import WorkflowStore


def test_machine_rejects_undeclared_edges_and_allows_staying():
    m = Machine("m", {"a": {"b"}, "b": set()})
    assert m.check("a", "b") == "b" and m.allows("b", "b") and m.terminal == {"b"}
    assert m.sources("b") == ("a", "b")
    with pytest.raises(IllegalTransition):
        m.check("b", "a")
    with pytest.raises(ValueError):
        Machine("broken", {"a": {"missing"}})


@pytest.mark.parametrize("machine", [APPROVAL, EXECUTION, ACTION, GRANT, INBOX, OUTBOX, STEP, CONNECTION, ALERT, FACT])
def test_every_state_is_reachable_from_the_initial_one(machine):
    initial = next(iter(machine.edges))
    seen, frontier = {initial}, [initial]
    while frontier:
        for target in machine.edges[frontier.pop()]:
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    # preexisting is written directly when the grant takes effect, not reached from active.
    assert seen | ({GrantStatus.PREEXISTING} if machine is GRANT else set()) == set(machine.edges)


def test_settled_rows_cannot_be_reopened():
    assert APPROVAL.terminal == {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED,
                                 ApprovalStatus.SUPERSEDED, ApprovalStatus.INVALIDATED}
    assert not ACTION.allows(ActionStatus.SUCCEEDED, ActionStatus.QUEUED)
    assert not ACTION.allows(ActionStatus.EXECUTING, ActionStatus.CANCELLED)  # running work is stopped by the worker, not cancelled
    assert not EXECUTION.allows(Execution.SUCCEEDED, Execution.EXECUTING)
    assert GRANT.sources(GrantStatus.REVOKED) == (GrantStatus.ACTIVE, GrantStatus.DRIFTED, GrantStatus.REVOKE_FAILED, GrantStatus.REVOKED)


def test_store_cas_refuses_an_undeclared_transition(tmp_path):
    store = WorkflowStore(f"sqlite:///{tmp_path / 'wf.sqlite3'}")
    try:
        with store.engine.begin() as db, pytest.raises(IllegalTransition):
            store._cas(db, {"id": "x", "version": 0, "status": ApprovalStatus.REJECTED}, status=ApprovalStatus.APPROVED)
        with store.engine.begin() as db, pytest.raises(IllegalTransition):
            store._action_cas(db, {"id": "x", "version": 0, "lease_owner": "w", "status": ActionStatus.SUCCEEDED}, status=ActionStatus.QUEUED)
    finally:
        store.close()


def test_plan_dict_reports_executed_step_status():
    plan = ExecutionPlan("p", "goal", [PlanStep("s1", "诊断", "network_software", []), PlanStep("s2", "建单", "ticket", ["s1"])])
    steps = plan.to_dict({"s1": StepStatus.FAILED, "s2": StepStatus.BLOCKED})["steps"]
    assert [s["status"] for s in steps] == ["failed", "blocked"]
    assert [s["status"] for s in plan.to_dict()["steps"]] == ["pending", "pending"]


def test_stream_without_terminal_event_is_reported_abandoned(monkeypatch):
    monkeypatch.setenv("IT_COPILOT_SSE_HEARTBEAT_S", "0.01")
    monkeypatch.setenv("IT_COPILOT_SSE_IDLE_TIMEOUT_S", "0.05")
    bus = InMemoryEventBus()
    monkeypatch.setattr(state, "events", bus)

    async def run():
        await bus.claim("chat:r", "alice")
        await bus.publish("chat:r", "status", {"stage": "started"})  # the run dies after this
        return [chunk async for chunk in chat_routes._subscribe("chat:r", None)]

    chunks = asyncio.run(run())
    assert chunks[0].startswith("id:") and "event: status" in chunks[0]
    assert "event: error" in chunks[-1] and "stream_abandoned" in chunks[-1]


def test_cancelled_probe_returns_agent_to_half_open(monkeypatch):
    monkeypatch.setenv("IT_COPILOT_MONITOR_COOLDOWN_SECONDS", "1")
    stats = AgentStats()
    stats.set_penalty(.9)
    assert stats.state() == AgentHealth.COOLING
    stats.retry_at -= 5
    assert stats.try_acquire("probe") and stats.state() == AgentHealth.PROBING and not stats.try_acquire("other")
    stats.abandon_probe("other")  # not the probe owner: no effect
    assert stats.state() == AgentHealth.PROBING
    stats.abandon_probe("probe")
    assert stats.state() == AgentHealth.HALF_OPEN and stats.try_acquire("next")


def test_alert_resolves_only_after_clean_ticks():
    alerts = AlertManager()
    alerts.evaluate({"k": Condition("rule", "warning", "m", 1, 0)})
    alerts.evaluate({})
    assert alerts.firing["k"].clearing
    alerts.evaluate({})
    assert "k" not in alerts.firing and alerts.history[-1].state == "resolved"


def test_profile_facts_are_deduplicated_bounded_and_superseded_only_for_single_values():
    profile = {}
    for _ in range(3):  # the same extraction again must not pile up duplicates
        profile = MemoryManager._merge_profile(profile, MemoryManager._normalize_profile({"department": "研发", "systems": ["GitLab"]}))
    assert len(profile["facts"]) == 2
    profile = MemoryManager._merge_profile(profile, MemoryManager._normalize_profile({"department": "产品", "systems": ["Jira"]}))
    status = {(f["key"], f["value"]): f["status"] for f in profile["facts"]}
    assert status[("department", "研发")] == FactStatus.SUPERSEDED and status[("department", "产品")] == FactStatus.ACTIVE
    assert status[("systems", "GitLab")] == FactStatus.ACTIVE  # list attributes accumulate
    back = MemoryManager._merge_profile(profile, MemoryManager._normalize_profile({"department": "研发"}))
    status = {(f["key"], f["value"]): f["status"] for f in back["facts"]}
    assert status[("department", "研发")] == FactStatus.ACTIVE and status[("department", "产品")] == FactStatus.SUPERSEDED
    many = MemoryManager._merge_facts(back["facts"], [{"key": "systems", "value": f"s{i}"} for i in range(80)])
    assert sum(f["status"] == FactStatus.ACTIVE for f in many) == MAX_ACTIVE_FACTS and many[-1]["value"] == "s79"
    text = MemoryContext([], [], back, "").to_prompt_text()
    assert "产品" not in text.split('"facts"')[1] and "研发" in text
