"""Real migrations, outbox locking and notifications in an isolated PostgreSQL schema."""
import asyncio
import os

import pytest
from sqlalchemy import select, text

from servicedesk_agent.infra.db import actions
from servicedesk_agent.workflow.store import WorkflowStore
from tests.test_workflow import BOB, create

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not os.getenv("SERVICEDESK_AGENT_TEST_DATABASE_URL"),
              reason="requires PostgreSQL with SERVICEDESK_AGENT_TEST_DATABASE_URL and pgvector")]


async def test_migrated_queue_skips_locked_work_and_never_claims_twice(db_url):
    store = WorkflowStore(db_url)
    try:
        for employee in ("1001", "2001", "2002"):
            pending = create(store, employee, actions=[{"tool_name": "create_ticket", "arguments": {
                "employee_id": "1001", "title": employee, "description": "test"}}])
            store.decide(employee, "approved", BOB, pending["payload_hash"])
        with store.engine.begin() as blocker:
            assert blocker.execute(text("select version_num from alembic_version")).scalar_one() == "0017"
            locked = blocker.execute(select(actions.c.id).order_by(actions.c.not_before).limit(1).with_for_update()).scalar_one()
            batches = await asyncio.wait_for(asyncio.gather(*[
                store.actions.claim(str(i), 60, 1) for i in range(4)]), 3)
            ids = [a["id"] for batch in batches for a in batch]
            assert len(ids) == len(set(ids)) == 2 and locked not in ids
        assert [a["id"] for a in await store.actions.claim("next", 60)] == [locked]
    finally:
        await store.aclose()


async def test_approval_commit_wakes_independent_listener(db_url):
    store = WorkflowStore(db_url)
    from servicedesk_agent.workflow.notifier import ActionNotifier
    listener = ActionNotifier(db_url)
    listener.start()
    try:
        await asyncio.wait_for(listener.event.wait(), 3)  # LISTEN is installed
        listener.event.clear()
        pending = create(store)
        await store.run(store.decide, "a", "approved", BOB, pending["payload_hash"])
        await asyncio.wait_for(listener.event.wait(), 1)
    finally:
        await listener.close()
        await store.aclose()
