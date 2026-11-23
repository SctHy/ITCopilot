"""Sandbox directory adapter backed by the workflow database's ``directory_*`` tables.

Each tenant has its own directory (the tenant is part of every key, ADR 0023); the adapter works on
the directory of the tenant in scope, which the worker sets to the action's tenant. Outside a
tenant scope every call fails instead of touching some tenant's directory.

Each effect and its operation id commit in one transaction, so ``observe`` is exact and
re-applying an action id returns the recorded result instead of repeating the effect.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from sqlalchemy.engine import Engine

from servicedesk_agent.workflow.database import WorkflowDatabase
from servicedesk_agent.workflow.directory import DirectoryRepo


class LocalDirectoryAdapter:
    name = "local"

    def __init__(self, engine: Engine | DirectoryRepo):
        self.directory = engine if isinstance(engine, DirectoryRepo) else DirectoryRepo(WorkflowDatabase(engine))
        self.database = self.directory.database

    @property
    def engine(self):
        return self.database.engine

    async def close(self):
        await self.database.async_engine.dispose()

    def level(self, role: str) -> str:
        return role

    async def preflight(self, action: Dict[str, Any]) -> None:
        await self.database.run(self.directory.preflight, action)

    async def current_level(self, employee_id: str, system: str) -> Optional[str]:
        return await self.database.run(self.directory.current_level, employee_id, system)

    async def observe(self, action_id: str, action: Dict[str, Any]) -> bool:
        return await self.database.run(self.directory.observe, action_id)

    async def apply(self, action_id: str, action: Dict[str, Any]) -> Dict[str, Any]:
        return await self.database.run(self.directory.apply, action_id, action)
