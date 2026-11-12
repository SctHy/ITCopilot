"""Feedback -> redacted snapshot -> two-person labels -> executable regression input."""
import asyncio
from dataclasses import replace
import hashlib
import json
import time

import fakeredis.aioredis
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from api import state
from api.main import app
from core.db import badcases, feedback, turn_records
from core.tenant_context import system_scope
from evaluation.badcase_taxonomy import Label
from evaluation.online_regression import export_cases
from evaluation.quality import quality_report
from memory.conversation_memory import MemoryManager
from tests.conftest import auth
from tests.test_workflow import ALICE, BOB, ERIN
from workflow.badcases import BadcaseConflict, BadcaseStore, requires_second
from workflow.badcase_miner import BadcaseMiner
from workflow.store import WorkflowStore


def turn(engine, rid="turn-1", *, principal=ALICE, signals=(), at=None, **extra):
    row = dict(request_id=rid, tenant=principal.tenant, owner=principal.scope_key, channel="web", conv_id="conversation",
        task_id=None, created_at=time.time() if at is None else at, status="success", error_code=None, outcome="success",
        intent="vpn_issue", intent_group="network", intent_source="llm", intent_confidence=.9,
        agent_spec="it_copilot", agent_version="1.0.0", model="test", route="agent_read", route_reason="primary",
        prompt_fp="p", skill_fp="s", tools_fp="t", policy_version="v1", kb_version="k1", build_sha="test-build",
        model_requests=1, input_tokens=100, output_tokens=20, cache_read_tokens=50, cost_usd=.01,
        latency_ms=100, first_token_ms=10, stage_ms={}, tools=[], signals=list(signals), approval_id=None,
        quality={"write_turn": True, "clarification_needed": False, "knowledge_used": False, "claim_corrected": False})
    row.update(extra)
    with system_scope(), engine.begin() as db:
        db.execute(turn_records.insert().values(**row))
    return row


@pytest.fixture
def store(db_url, monkeypatch):
    store = WorkflowStore(db_url)
    monkeypatch.setattr(state, "workflow", store)
    return store


async def memory(db_url, monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("memory.conversation_memory.redis.from_url", lambda *a, **kw: client)
    return MemoryManager("redis://unused", db_url, api_key="sk-local")


async def commit(manager, row, text="VPN 连不上，密码是 Abc!2026x", reply="请检查账号状态。", missing=()):
    await manager.commit_turn(row["owner"], row["conv_id"], row["request_id"], text,
        {"request_id": row["request_id"], "response": reply, "status": "success"},
        trace={"turn": {"user": text, "assistant": reply, "missing_slots": list(missing)}})


def label():
    return Label(root_cause="false_claim", severity="P2", expected={"outcome": "free", "forbid_text": ["已执行"]}, note="不应虚假声称完成")


def test_feedback_requires_owner_in_same_tenant_and_upserts_redacted_comment(store):
    row = turn(store.engine)
    http = TestClient(app)
    path = "/chat/" + row["request_id"] + "/feedback"
    payload = {"rating": -1, "reason": "false_claim", "comment": "我的密码是 Abc!2026x"}
    assert http.post(path, json=payload, headers=auth("erin")).status_code == 404
    assert http.post(path, json=payload, headers=auth("alice", tenant="other")).status_code == 404
    assert http.post(path, json=payload, headers=auth("alice")).status_code == 200
    assert http.post(path, json={"rating": 1}, headers=auth("alice")).status_code == 200
    with store.engine.connect() as db:
        saved = db.execute(select(feedback)).mappings().one()
    assert saved["rating"] == 1 and saved["comment"] == ""
    assert http.post(path, json={"rating": 0}, headers=auth("alice")).status_code == 422


def test_feedback_truncates_after_redaction_expands_a_maximum_length_comment(store):
    row = turn(store.engine, "feedback-length")
    comment = "密码: Ab9 " + "补" * 492
    assert len(comment) == 500
    response = TestClient(app).post("/chat/" + row["request_id"] + "/feedback",
        json={"rating": -1, "reason": "unsafe", "comment": comment}, headers=auth("alice"))
    assert response.status_code == 200, response.text
    with store.engine.connect() as db:
        saved = db.execute(select(feedback).where(feedback.c.request_id == row["request_id"])).mappings().one()
    assert "Ab9" not in saved["comment"]
    assert saved["comment"].startswith("密码: [REDACTED] ")
    assert len(saved["comment"]) == 500


def test_signal_and_negative_feedback_create_complete_redacted_snapshots(store, db_url, monkeypatch):
    async def run():
        manager = await memory(db_url, monkeypatch)
        first = turn(store.engine, signals=["claim_noted"])
        second = turn(store.engine, "feedback-case")
        await commit(manager, first)
        await commit(manager, second, text="VPN 客户端升级失败")
        BadcaseStore(store.engine).feedback(second["request_id"], ALICE, -1, "not_done", "手机13800138000")
        miner = BadcaseMiner(store.engine, manager)
        report = await miner.run_once()
        assert report["signal"] == report["feedback"] == 1
        with store.engine.connect() as db:
            rows = [dict(r) for r in db.execute(select(badcases)).mappings()]
        assert len(rows) == 2 and all(not row["snapshot"]["snapshot_partial"] for row in rows)
        assert "Abc!2026x" not in json.dumps(rows) and "13800138000" not in json.dumps(rows)
        await miner.run_once()
        with store.engine.connect() as db:
            assert len(db.execute(select(badcases)).all()) == 2
        await manager.close()
    asyncio.run(run())


def test_label_promote_and_export_are_tenant_scoped(store, db_url, monkeypatch):
    async def run():
        manager = await memory(db_url, monkeypatch)
        row = turn(store.engine, signals=["claim_noted"])
        await commit(manager, row, text="我的工号912345，VPN账号连接失败")
        await BadcaseMiner(store.engine, manager).run_once()
        with store.engine.connect() as db:
            case_id = db.scalar(select(badcases.c.id))
        http = TestClient(app)
        assert http.get("/admin/badcases", headers=auth("alice")).status_code == 403
        assert http.get(f"/admin/badcases/{case_id}", headers=auth("erin", tenant="other")).status_code == 404
        assert http.post(f"/admin/badcases/{case_id}/promote", headers=auth("erin")).status_code == 409
        assert http.post(f"/admin/badcases/{case_id}/label", json=label().model_dump(), headers=auth("erin")).status_code == 200
        response = http.post(f"/admin/badcases/{case_id}/promote", headers=auth("erin"))
        assert response.status_code == 200, response.text
        exported = export_cases([response.json()])
        assert exported[0]["outcome"] == "free" and exported[0]["turns"] == ["我的工号1001，VPN账号连接失败"]
        assert exported[0]["forbid_text"] == ["已执行"]
        assert http.post(f"/admin/badcases/{case_id}/dismiss", headers=auth("erin")).status_code == 409
        await manager.close()
    asyncio.run(run())


def test_sampled_double_label_requires_different_people_and_reports_kappa(store):
    rid = next(f"sample-{i}" for i in range(10000) if int(hashlib.sha256(f"sample-{i}".encode()).hexdigest(), 16) % 100 == 0 and requires_second({"source": "sampled", "request_id": f"sample-{i}"}))
    row = turn(store.engine, rid)
    snapshot = {"snapshot_partial": False, "turn_record": row, "turns": ["VPN失败"], "response": "重试", "trace": None}
    miner = BadcaseMiner(store.engine)
    miner._save(row, set(), "sampled", snapshot)
    case_id = hashlib.sha256(rid.encode()).hexdigest()
    cases = BadcaseStore(store.engine)
    assert requires_second(cases.get(case_id, ALICE.tenant))
    cases.label(case_id, ERIN, label())
    with pytest.raises(BadcaseConflict, match="第二人"):
        cases.label(case_id, ERIN, label())
    with pytest.raises(BadcaseConflict, match="第二人"):
        cases.transition(case_id, ERIN, "promoted")
    cases.label(case_id, replace(ERIN, subject="reviewer-2"), label())
    saved = cases.transition(case_id, ERIN, "promoted")
    report = quality_report([row], [], [saved])
    assert report["label_quality"]["n"] == 1 and report["label_quality"]["agreement"] == 1


def test_missing_trace_is_partial_and_cannot_be_promoted(store):
    row = turn(store.engine, signals=["claim_noted"])
    asyncio.run(BadcaseMiner(store.engine).run_once())
    cases = BadcaseStore(store.engine)
    case_id = hashlib.sha256(row["request_id"].encode()).hexdigest()
    cases.label(case_id, ERIN, label())
    with pytest.raises(BadcaseConflict, match="快照正文缺失"):
        cases.transition(case_id, ERIN, "promoted")


def test_quality_metrics_do_not_invent_cost_or_small_sample_values(store):
    row = turn(store.engine, cost_usd=None)
    report = quality_report([row], [], [])
    assert report["cost"]["per_turn_usd"] is None
    assert report["experience"]["negative_feedback"]["value"] is None
    assert report["experience"]["latency_ms"]["p99"] == 100
    assert quality_report([], [], [])["experience"]["latency_ms"]["p99"] is None


def test_shared_task_observer_can_feedback_but_nonmembers_and_other_tenants_cannot(store, monkeypatch):
    from core.db import task_members
    from workflow.tasks import TaskStore, key
    from workflow.task_access import TaskAccess
    tasks = TaskStore(store.engine)
    tasks.access = TaskAccess(tasks)
    monkeypatch.setattr(state, "tasks", tasks)
    room = tasks.create(ALICE, "设备排障", "VPN连接失败", "task-feedback")
    claim = tasks.claim()
    row = turn(store.engine, claim["id"], task_id=room["id"], channel="task", conv_id="task:" + room["id"])
    tasks.finish(claim, {"response": "请检查账号状态"})
    with store.engine.begin() as db:
        db.execute(task_members.insert().values(task_id=room["id"], subject=BOB.subject, role="observer", profile=BOB.public()))
    path = f"/tasks/{room['id']}/events/{key('reply', claim['id'])}/feedback"
    http = TestClient(app)
    assert http.post(path, json={"rating": -1}, headers=auth("bob")).status_code == 200
    assert http.post(path, json={"rating": -1}, headers=auth("carol")).status_code == 403
    assert http.post(path, json={"rating": -1}, headers=auth("bob", tenant="other")).status_code == 403
    with store.engine.connect() as db:
        saved = db.execute(select(feedback).where(feedback.c.request_id == row["request_id"])).mappings().one()
    assert saved["actor"] == BOB.scope_key


def test_miner_discovers_clarification_loops_and_rephrasing(store, db_url, monkeypatch):
    async def run():
        manager = await memory(db_url, monkeypatch)
        now = time.time()
        for i in range(3):
            row = turn(store.engine, f"clarify-{i}", at=now + i)
            await commit(manager, row, text="VPN客户端报错691", missing=["system"])
        await BadcaseMiner(store.engine, manager, clock=lambda: now + 10).run_once()
        with store.engine.connect() as db:
            rows = list(db.execute(select(badcases)).mappings())
        assert any("clarify_loop" in row["signals"] for row in rows)
        assert any("rephrase" in row["signals"] for row in rows)
        await manager.close()
    asyncio.run(run())


def test_success_sample_and_second_label_use_independent_buckets():
    sampled = [{"request_id": f"sample-{i}", "source": "sampled"} for i in range(10000)
        if int(hashlib.sha256(f"sample-{i}".encode()).hexdigest(), 16) % 100 == 0]
    second = sum(requires_second(row) for row in sampled)
    assert .05 <= second / len(sampled) <= .2


def test_final_task_failure_record_can_be_replaced_by_authoritative_recovery(store):
    from core.turn_records import record_turn
    row = turn(store.engine, "recovered", status="failed", error_code="model_unavailable", outcome="model_unavailable", signals=["model_unavailable"])
    with store.engine.begin() as db:
        record_turn(db, {**row, "status": "success", "error_code": None, "outcome": "success", "signals": []})
    with store.engine.connect() as db:
        saved = db.execute(select(turn_records).where(turn_records.c.request_id == row["request_id"])).mappings().one()
    assert saved["outcome"] == "success" and saved["signals"] == []


def test_miner_shutdown_drains_its_database_thread(store):
    from threading import Event
    from sqlalchemy import event
    entered, release = Event(), Event()
    def hold_query(connection, cursor, statement, parameters, context, executemany):
        if "FROM turn_records" in statement:
            entered.set()
            assert release.wait(5), "test did not release the database query"
    event.listen(store.engine, "before_cursor_execute", hold_query)
    async def run():
        miner = BadcaseMiner(store.engine)
        miner.start()
        stopping = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            stopping = asyncio.create_task(miner.stop())
            await asyncio.sleep(0)
            assert not stopping.done()
        finally:
            release.set()
            if stopping is None:
                await miner.stop()
            else:
                await asyncio.wait_for(stopping, 3)
        assert miner._task is None
    try:
        asyncio.run(run())
    finally:
        event.remove(store.engine, "before_cursor_execute", hold_query)
