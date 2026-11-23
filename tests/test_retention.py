"""Retention sweep: old IM text and episodic memory go; audit records, failures and anything an open approval needs stay."""
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

from tests import runtime as api
from tests.runtime import state
from servicedesk_agent.infra.db import audit, vector_documents
from servicedesk_agent.infra.retention import DAY, RetentionPolicy, RetentionSweeper
from tests.conftest import auth
from tests.test_workflow import ALICE, Clock, access
from servicedesk_agent.workflow.store import WorkflowStore

NOW = 1_800_000_000.0


@pytest.fixture
def env(db_url):
    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    return store, RetentionSweeper(store.engine, RetentionPolicy(episodic_days=90, batch=2), clock=clock), clock


def memory(db, id_, *, collection="episodic", age_days=100):
    db.execute(insert(vector_documents).values(collection=collection, id=id_, document="用户问过 VPN", meta={}, embedding=np.ones(4, dtype=np.float32),
                                               model="m", updated_at=NOW - age_days * DAY))


def ids(store, table):
    with store.engine.connect() as db:
        return set(db.execute(select(table.c.id)).scalars())


def test_old_settled_messages_and_memories_are_deleted_in_batches(env):
    store, sweeper, _ = env
    with store.engine.begin() as db:
        memory(db, "turn-old")
        memory(db, "turn-recent", age_days=30)
        memory(db, "profile-old", collection="profile")      # merged in place, never swept
        memory(db, "kb-old", collection="knowledge_base")
    report = sweeper.sweep()
    assert report == {"episodic_memory": 1, "turn_records": 0, "chat_history": 0, "chat_conversations": 0}
    with store.engine.connect() as db:
        left = set(db.execute(select(vector_documents.c.collection, vector_documents.c.id)).all())
    assert left == {("episodic", "turn-recent"), ("profile", "profile-old"), ("knowledge_base", "kb-old")}
    assert sweeper.sweep() == {"episodic_memory": 0, "turn_records": 0, "chat_history": 0, "chat_conversations": 0}


def test_audit_and_workflow_records_are_never_swept(env):
    store, sweeper, clock = env
    store.create("ap", request_id="r", conv_id="c", principal=ALICE, action_requests=[access()])
    clock.t += 1000 * DAY
    sweeper.sweep()
    assert store.get("ap") is not None
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(audit)).scalar() >= 1



def test_admin_can_run_a_sweep(env, monkeypatch):
    _, sweeper, _ = env
    monkeypatch.setattr(state, "retention", sweeper)
    http = TestClient(api.app)
    assert http.post("/ops/retention/run", headers=auth("alice")).status_code == 403
    body = http.post("/ops/retention/run", headers=auth("erin")).json()
    assert body["deleted"] == {"episodic_memory": 0, "turn_records": 0, "chat_history": 0, "chat_conversations": 0}
    assert body["policy"] == {"episodic_days": 90, "turn_days": 90, "history_days": 90}
