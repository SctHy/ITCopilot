"""Ports at the task boundary. Integrations own domain-specific reads and effects."""
from typing import Protocol


class TaskEffects(Protocol):
    def view(self, task: dict) -> dict:
        """Extra read-model fields and an optional idle phase."""
        ...

    def commit(self, connection, task: dict, event: dict, claim: dict, result: dict) -> dict:
        """Stage effects on the task transaction; return public reply metadata."""
        ...


class NoTaskEffects:
    def view(self, task):
        return {}

    def commit(self, connection, task, event, claim, result):
        if result.get("proposal"):
            raise ValueError("该任务执行器未配置业务变更适配器")
        return {}


class TaskEventSource(Protocol):
    def poll(self) -> None:
        """Deliver durable, deduplicated events; a failed poll can be retried."""
        ...


class CompositeTaskEffects:
    def __init__(self, effects):
        self.effects = effects

    def view(self, task):
        result = {}
        for effect in self.effects:
            result.update(effect.view(task))
        return result

    def commit(self, connection, task, event, claim, result):
        metadata = {}
        for effect in self.effects:
            metadata.update(effect.commit(connection, task, event, claim, result))
        return metadata
