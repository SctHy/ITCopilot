"""Conversation readiness and the transient agent protocols."""
from enum import StrEnum

from core.states import Machine, Protocol


class SlotState(StrEnum):
    EMPTY = "empty"
    CLARIFYING = "clarifying"
    READY = "ready"


SLOTS = Machine(SlotState, """
    empty -> clarifying | ready
    clarifying -> ready
    ready -> clarifying
""")


def slot_state(state):
    if not state:
        return SlotState.EMPTY
    return SlotState.CLARIFYING if state.get("missing_fields") else SlotState.READY


TURN = Protocol("chat_turn", "new", {
    "new": {"run": "running", "replay": "replayed"},
    "running": {"propose": "approval", "respond": "responding", "fail": "failed", "cancel": "cancelled"},
    "approval": {"respond": "responding", "fail": "failed", "cancel": "cancelled"},
    "responding": {"commit": "committed", "finish": "finished", "fail": "failed", "cancel": "cancelled"},
    "committed": {"finish": "finished", "fail": "failed", "cancel": "cancelled"},
})

MODEL_RUN = Protocol("model_run", "new", {
    "new": {"run": "generating"},
    "generating": {"correct": "correcting", "complete": "completed", "truncate": "partial", "fail": "failed", "cancel": "cancelled"},
    "correcting": {"complete": "completed", "truncate": "partial", "fail": "failed", "cancel": "cancelled"},
})

TOOL_CALL = Protocol("tool_call", "new", {
    "new": {"validate": "validating"},
    "validating": {"execute": "executing", "fail": "failed", "cancel": "cancelled"},
    "executing": {"complete": "completed", "fail": "failed", "cancel": "cancelled"},
})

class OnceState(StrEnum):
    AVAILABLE = "available"
    CONSUMED = "consumed"


def once_state(key, claimed):
    return OnceState.CONSUMED if key in claimed else OnceState.AVAILABLE
