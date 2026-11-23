"""Conversation readiness derived from the collected slots."""
from enum import StrEnum

from servicedesk_agent.infra.states import Machine


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
