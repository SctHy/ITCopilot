"""Declared transition tables and the places that enforce them."""
import asyncio
from enum import StrEnum

import pytest

from api import state
from api.routes import chat as chat_routes
from core.events import InMemoryEventBus
from core.states import IllegalTransition, Machine
from intake.im import INBOX, OUTBOX
from memory.conversation_memory import MemoryContext, MemoryManager
from monitor.performance_monitor import ALERT, AlertManager, Condition
from tooling.mcp_client import CONNECTION
from workflow.states import ACTION, APPROVAL, EXECUTION, GRANT, ActionStatus, ApprovalStatus, Execution, GrantStatus
from workflow.store import WorkflowStore


class Light(StrEnum):
    A = "a"
    B = "b"


def test_machine_rejects_undeclared_edges_and_allows_staying():
    m = Machine(Light, "a -> b  # comment")
    assert m.check("a", "b") == "b" and m.allows("b", "b") and m.terminal == {"b"}
    assert m.sources("b") == ("a", "b")
    with pytest.raises(IllegalTransition):
        m.check("b", "a")
    with pytest.raises(ValueError):
        Machine(Light, "a -> missing")


@pytest.mark.parametrize("machine", [APPROVAL, EXECUTION, ACTION, GRANT, INBOX, OUTBOX, CONNECTION, ALERT])
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
                                 ApprovalStatus.SUPERSEDED, ApprovalStatus.INVALIDATED, ApprovalStatus.WITHDRAWN}
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


def test_alert_resolves_only_after_clean_ticks():
    alerts = AlertManager()
    alerts.evaluate({"k": Condition("rule", "warning", "m", 1, 0)})
    alerts.evaluate({})
    assert alerts.firing["k"].clearing
    alerts.evaluate({})
    assert "k" not in alerts.firing and alerts.history[-1].state == "resolved"


def test_profile_is_revised_not_accumulated():
    profile = {}
    for _ in range(3):  # the same revision again must not pile up
        profile = MemoryManager._merge_profile(profile, MemoryManager._normalize_profile({"background": "研发部", "work": "GitLab"}))
    profile = MemoryManager._merge_profile(profile, MemoryManager._normalize_profile({"background": "产品部"}))
    text = MemoryContext([], [], profile, "").to_prompt_text()
    assert text.count("GitLab") == 1 and "研发" not in text and "背景：产品部" in text
