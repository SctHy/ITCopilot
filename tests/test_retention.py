"""Retention sweep: old IM text and episodic memory go; audit records, failures and anything an open approval needs stay."""
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, insert, select

import api.main as api
from api import state
from core.db import audit, channel_inbox, channel_outbox, shared_engine, vector_documents
from core.retention import DAY, INBOX_SWEPT, OUTBOX_SWEPT, RetentionPolicy, RetentionSweeper
from intake.im import InboxStatus, OutboxStatus
from tests.conftest import auth
from tests.test_workflow import ALICE, BOB, Clock, access
from workflow.store import WorkflowStore

NOW = 1_800_000_000.0


@pytest.fixture
def env(db_url):
    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    return store, RetentionSweeper(store.engine, RetentionPolicy(channel_days=30, episodic_days=90, batch=2), clock=clock), clock


def inbox(db, id_, *, status="done", age_days=40, request_id=None):
    at = NOW - age_days * DAY
    db.execute(insert(channel_inbox).values(id=id_, channel="feishu", conv_id="feishu:oc", sender={}, text="我的工号 1001，VPN 连不上",
                                            address={}, status=status, attempts=1, not_before=at, version=1, request_id=request_id,
                                            created_at=at, updated_at=at))


def outbox(db, id_, *, status="sent", age_days=40):
    at = NOW - age_days * DAY
    db.execute(insert(channel_outbox).values(id=id_, channel="feishu", conv_key="feishu:oc", seq=1, address={}, text="已处理", status=status,
                                             attempts=1, not_before=at, version=1, created_at=at, sent_at=at))


def memory(db, id_, *, collection="episodic", age_days=100):
    db.execute(insert(vector_documents).values(collection=collection, id=id_, document="用户问过 VPN", meta={}, embedding=np.ones(4, dtype=np.float32),
                                               model="m", updated_at=NOW - age_days * DAY))


def ids(store, table):
    with store.engine.connect() as db:
        return set(db.execute(select(table.c.id)).scalars())


def test_swept_states_match_the_channel_state_machines():
    assert set(INBOX_SWEPT) < {s.value for s in InboxStatus} and InboxStatus.FAILED not in INBOX_SWEPT
    assert set(OUTBOX_SWEPT) < {s.value for s in OutboxStatus} and OutboxStatus.FAILED not in OUTBOX_SWEPT


def test_old_settled_messages_and_memories_are_deleted_in_batches(env):
    store, sweeper, _ = env
    with store.engine.begin() as db:
        for i in range(5):
            inbox(db, f"old-{i}")
        inbox(db, "recent", age_days=10)
        inbox(db, "failed", status="failed")                 # an operator still has to look at it
        inbox(db, "processing", status="processing")
        outbox(db, "sent-old")
        outbox(db, "dropped-old", status="dropped")
        outbox(db, "queued-old", status="queued")            # still to be delivered
        outbox(db, "sent-recent", age_days=5)
        memory(db, "turn-old")
        memory(db, "turn-recent", age_days=30)
        memory(db, "profile-old", collection="profile")      # merged in place, never swept
        memory(db, "kb-old", collection="knowledge_base")
    report = sweeper.sweep()
    assert report == {"channel_inbox": 5, "channel_outbox": 2, "episodic_memory": 1, "turn_records": 0}
    assert ids(store, channel_inbox) == {"recent", "failed", "processing"}
    assert ids(store, channel_outbox) == {"queued-old", "sent-recent"}
    with store.engine.connect() as db:
        left = set(db.execute(select(vector_documents.c.collection, vector_documents.c.id)).all())
    assert left == {("episodic", "turn-recent"), ("profile", "profile-old"), ("knowledge_base", "kb-old")}
    assert sweeper.sweep() == {"channel_inbox": 0, "channel_outbox": 0, "episodic_memory": 0, "turn_records": 0}


def test_messages_behind_an_open_approval_are_kept_until_it_settles(env):
    store, sweeper, _ = env
    pending = store.create("ap", request_id="rid-open", conv_id="feishu:oc", principal=ALICE, action_requests=[access()])
    with store.engine.begin() as db:
        inbox(db, "asks-for-access", request_id="rid-open")
    assert sweeper.sweep()["channel_inbox"] == 0          # the settle notice still needs the reply address
    store.decide("ap", "rejected", BOB, pending["payload_hash"])
    assert sweeper.sweep()["channel_inbox"] == 1
    # The decision record outlives the message that started it.
    assert {e["event"] for e in store.audit("ap")} >= {"created", "rejected"}


def test_audit_and_workflow_records_are_never_swept(env):
    store, sweeper, clock = env
    store.create("ap", request_id="r", conv_id="c", principal=ALICE, action_requests=[access()])
    clock.t += 1000 * DAY
    sweeper.sweep()
    assert store.get("ap") is not None
    with store.engine.connect() as db:
        assert db.execute(select(func.count()).select_from(audit)).scalar() >= 1


def test_zero_days_disables_a_kind(db_url, monkeypatch):
    monkeypatch.setenv("IT_COPILOT_RETENTION_CHANNEL_DAYS", "0")
    monkeypatch.setenv("IT_COPILOT_RETENTION_EPISODIC_DAYS", "7")
    policy = RetentionPolicy.from_env()
    assert policy.channel_days is None and policy.episodic_days == 7
    engine = shared_engine(db_url)
    WorkflowStore(db_url)  # schema
    with engine.begin() as db:
        inbox(db, "very-old", age_days=10_000)
    assert RetentionSweeper(engine, policy, clock=lambda: NOW).sweep()["channel_inbox"] == 0


def test_admin_can_run_a_sweep(env, monkeypatch):
    _, sweeper, _ = env
    monkeypatch.setattr(state, "retention", sweeper)
    http = TestClient(api.app)
    assert http.post("/ops/retention/run", headers=auth("alice")).status_code == 403
    body = http.post("/ops/retention/run", headers=auth("erin")).json()
    assert body["deleted"] == {"channel_inbox": 0, "channel_outbox": 0, "episodic_memory": 0, "turn_records": 0}
    assert body["policy"] == {"channel_days": 30, "episodic_days": 90, "turn_days": 90}
