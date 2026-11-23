"""Declared transition tables and the places that enforce them."""
import asyncio
from enum import StrEnum

import pytest

from tests.runtime import state
from it_copilot.api.routes import chat as chat_routes
from it_copilot.infra.events import InMemoryEventBus
from it_copilot.infra.states import IllegalTransition, Machine
from it_copilot.workflow.states import ACTION, APPROVAL, EXECUTION, GRANT, ActionStatus, ApprovalStatus, Execution, GrantStatus
from it_copilot.workflow.store import WorkflowStore


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


@pytest.mark.parametrize("machine", [APPROVAL, EXECUTION, ACTION, GRANT])
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
        return [chunk async for chunk in chat_routes._subscribe(state, "chat:r", None)]

    chunks = asyncio.run(run())
    assert chunks[0].startswith("id:") and "event: status" in chunks[0]
    assert "event: error" in chunks[-1] and "stream_abandoned" in chunks[-1]
