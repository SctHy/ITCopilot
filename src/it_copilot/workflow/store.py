from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from sqlalchemy import insert, select

from it_copilot.infra import db as schema
from it_copilot.infra.db import (
    approvals,
    audit,
)
from it_copilot.workflow.actions import ActionQueue
from it_copilot.workflow.approvals import ApprovalRepo
from it_copilot.workflow.directory import DirectoryRepo
from it_copilot.workflow.grants import GrantLedger
from it_copilot.workflow.policy import AccessPolicy, load_policy
from it_copilot.workflow.repository import (
    SEED_EMPLOYEES,
    ApprovalConflict,
    ApprovalForbidden,
    ApprovalStateConflict,
    LeaseLost,
)

from it_copilot.workflow.database import WorkflowDatabase
from it_copilot.workflow.notifier import ActionNotifier

__all__ = ["WorkflowStore", "ApprovalForbidden", "ApprovalConflict", "ApprovalStateConflict", "LeaseLost", "SEED_EMPLOYEES"]


class WorkflowStore:
    @property
    def engine(self):
        return self.database.engine

    async def run(self, operation, *args, **kwargs):
        return await self.database.run(operation, *args, **kwargs)

    async def run_with_engine(self, operation, *args, **kwargs):
        return await self.run(lambda: operation(self.engine, *args, **kwargs))

    async def aclose(self):
        await self.database.async_engine.dispose()
        self.close()

    def __init__(self, url: str, *, policy: Optional[AccessPolicy] = None, clock: Callable[[], float] = time.time,
                 route: Callable[[Dict[str, Any]], str] = lambda action: "local", run_migrations: bool = True,
                 notify: Optional[Callable[[Any, Dict[str, Any]], None]] = None):
        self.url = url
        self.sync_engine = schema.shared_engine(url) if run_migrations else schema.make_engine(url)
        self.database = WorkflowDatabase(self.sync_engine)
        self.notifier = ActionNotifier(url)
        self._policy = policy
        self.clock = clock
        self.route = route
        # Called inside the transaction that settles an approval (rejected, expired, invalidated,
        # execution finished), so a channel can queue its notice atomically with the state change.
        self.notify = notify
        self.approvals = ApprovalRepo(self)
        self.actions = ActionQueue(self)
        self.grants = GrantLedger(self)
        self.directory = DirectoryRepo(self.database)
        self.directory._seed()

    @property
    def policy(self) -> AccessPolicy:
        """The injected policy, or the policy file as it is now (edits apply without restart)."""
        return self._policy or load_policy()

    @policy.setter
    def policy(self, value: AccessPolicy) -> None:
        self._policy = value

    def transaction(self):
        return self.notifier.transaction(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    def _audit(self, db, approval_id, event, actor, *, action_id=None, detail=None) -> None:
        # The entry takes the approval's tenant; an approval this scope cannot see yields NULL and the
        # NOT NULL column refuses the entry rather than filing it under some other tenant.
        tenant = select(approvals.c.tenant).where(approvals.c.id == approval_id).scalar_subquery()
        db.execute(insert(audit).values(tenant=tenant, approval_id=approval_id, action_id=action_id, event=event, actor=actor[:200],
                                        detail=detail, at=self.clock()))

    def _notify(self, db, approval_id: str, event: str, detail: Optional[Dict[str, Any]] = None) -> None:
        if not self.notify:
            return
        row = db.execute(select(approvals.c.request_id, approvals.c.conv_id, approvals.c.requester)
                         .where(approvals.c.id == approval_id)).mappings().first()
        self.notify(db, {"approval_id": approval_id, "event": event, "detail": detail or {}, **row})

    def __getattr__(self, name):
        """Compatibility surface for existing synchronous consumers."""
        for repo in (self.approvals, self.actions.transactions, self.grants, self.directory):
            if hasattr(type(repo), name):
                return getattr(repo, name)
        raise AttributeError(name)
