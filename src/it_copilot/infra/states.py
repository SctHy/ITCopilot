"""Declared state machines: a status enum plus its transition diagram, checked on every write.

Status values stay plain strings in the database and API (``StrEnum`` members compare equal
to them). The diagram is one line per source state, ``a -> b | c``; states with no line are
terminal. Staying in the same state is always allowed (a vote short of quorum, a lease
reclaimed by another worker).
"""
from __future__ import annotations

from enum import StrEnum
from typing import Dict, FrozenSet, Tuple, Type


class IllegalTransition(RuntimeError):
    pass


class Machine:
    def __init__(self, states: Type[StrEnum], diagram: str):
        self.name = states.__name__
        self.edges: Dict[str, FrozenSet[str]] = {s: frozenset() for s in states}
        for line in filter(None, (line.split("#")[0].strip() for line in diagram.splitlines())):
            source, _, targets = (part.strip() for part in line.partition("->"))
            self.edges[states(source)] = frozenset(states(t.strip()) for t in targets.split("|"))
        self.terminal = frozenset(s for s, targets in self.edges.items() if not targets)

    def allows(self, old: str, new: str) -> bool:
        return old in self.edges and new in self.edges and (old == new or new in self.edges[old])

    def check(self, old: str, new: str) -> str:
        if not self.allows(old, new):
            raise IllegalTransition(f"{self.name}: {old} -> {new} is not a declared transition")
        return new

    def sources(self, target: str) -> Tuple[str, ...]:
        """States that may move to ``target``; a WHERE guard for bulk updates."""
        return tuple(sorted(s for s, targets in self.edges.items() if s == target or target in targets))


class Protocol:
    """Event-labelled transitions for transient protocols; no second copy of durable rows.

    Unlike Machine's idempotent row writes, an event must be explicitly declared even
    for a self-loop. A Flow belongs to one operation, never a global singleton.
    """

    def __init__(self, name, initial, transitions):
        self.name, self.initial = name, initial
        self.transitions = transitions
        self.states = frozenset({initial, *transitions, *(v for row in transitions.values() for v in row.values())})

    def start(self):
        return Flow(self)


class Flow:
    def __init__(self, protocol):
        self.protocol = protocol
        self.state = protocol.initial

    def send(self, event):
        target = self.protocol.transitions.get(self.state, {}).get(event)
        if target is None:
            raise IllegalTransition(f"{self.protocol.name}: {self.state} + {event} is not declared")
        self.state = target
        return target
