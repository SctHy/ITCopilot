"""Declared state machines: one transition table per persisted status column.

Status values stay plain strings in the database and API (``StrEnum`` members compare
equal to them); the table is what writers check before they move a row, so an edge
that is not declared here fails loudly instead of silently corrupting a lifecycle.
Staying in the same state is always allowed (a vote that does not reach quorum, a
lease reclaimed by another worker).
"""
from __future__ import annotations

from typing import Iterable, Mapping, Tuple


class IllegalTransition(RuntimeError):
    pass


class Machine:
    def __init__(self, name: str, edges: Mapping[str, Iterable[str]]):
        self.name = name
        self.edges = {str(state): frozenset(map(str, targets)) for state, targets in edges.items()}
        unknown = {t for targets in self.edges.values() for t in targets} - set(self.edges)
        if unknown:
            raise ValueError(f"{name}: targets without a row in the table: {sorted(unknown)}")
        self.terminal = frozenset(state for state, targets in self.edges.items() if not targets)

    def allows(self, old: str, new: str) -> bool:
        return old == new or new in self.edges.get(old, ())

    def check(self, old: str, new: str) -> str:
        if not self.allows(old, new):
            raise IllegalTransition(f"{self.name}: {old} -> {new} is not a declared transition")
        return new

    def sources(self, target: str) -> Tuple[str, ...]:
        """States that may move to ``target``; use as a WHERE guard on bulk updates."""
        return tuple(sorted(state for state, targets in self.edges.items() if state == target or target in targets))
