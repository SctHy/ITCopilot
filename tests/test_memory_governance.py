"""写入边界、跨主体隔离和近重复更新走真实向量表。"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest
from sqlalchemy import select

from agents.intent.recognizer import IntentResult
from agents.intent.taxonomy import IntentCategory, UrgencyLevel
from api.schemas import ChatResponse
from core.db import admin_audit, read, vector_documents
from core.redaction import redact_text
from memory.admission import admit_turn
from memory.conversation_memory import MemoryManager, profile_text
from scripts.redact_existing_memory import redact_existing_memory
from core.vector_store import VectorStore


def memory(url):
    manager = MemoryManager(database_url=url, api_key="local-test")
    manager._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return manager


@pytest.mark.parametrize("secret", ["密码是 Abc!2026", "验证码：738192", "password=bad",
    "13812345678", "11010519491231002X", "4111111111111111", "sk-abcdefghijklmnop", "Bearer abc.def.ghi"])
def test_redaction_secrets_and_business_identifiers(secret):
    safe = redact_text(f"工号1001 设备 DEV-1001 {secret}")
    assert secret not in safe and "[REDACTED]" in safe
    assert "工号1001" in safe and "DEV-1001" in safe
    assert redact_text("订单1234567890123") == "订单1234567890123"


def intent():
    return IntentResult(intent=IntentCategory.VPN_ISSUE, confidence=.9, intent_group="network", urgency=UrgencyLevel.NORMAL)


@pytest.mark.parametrize("status,category,missing,tools,knowledge,admitted", [
    ("failed", "vpn_issue", [], ["diagnose"], True, False),
    ("success", "chitchat", [], [], False, False),
    ("success", "out_of_scope", [], ["diagnose"], False, False),
    ("success", "vpn_issue", ["employee_id"], ["diagnose"], True, False),
    ("success", "vpn_issue", [], [], False, False),
    ("success", "vpn_issue", [], ["diagnose"], False, True),
])
def test_admission_behavior(status, category, missing, tools, knowledge, admitted):
    response = ChatResponse(request_id="r", response="结论", status=status, intent=category,
        tools_used=tools, knowledge_used=knowledge)
    record = admit_turn(response, intent(), {"missing_fields": missing})
    assert (record is not None) is admitted


def test_episodic_dedup_is_idempotent_and_scoped(db_url):
    async def run():
        manager = memory(db_url)
        for request in ("r1", "r2", "r3", "r3"):
            await manager.store_episodic_turn("u", "c", request, "VPN 691 失败", "检查账号", {})
        await manager.store_episodic_turn("other", "c", "r4", "VPN 691 失败", "检查账号", {})
        rows = manager._episodic.get(where={"user_id": "u"})
        assert len(rows["ids"]) == 1 and rows["metadatas"][0]["seen"] == 3
        assert manager._episodic.count() == 2
        await manager.close()
    asyncio.run(run())


def test_working_memory_trace_and_receipt_do_not_store_secret(db_url):
    async def run():
        manager = memory(db_url)
        await manager.commit_turn("u", "c", "r", "密码是 Abc!2026", {
            "request_id": "r", "response": "你的密码是 Abc!2026", "status": "success"},
            {"text": "手机13812345678"}, state={"missing_fields": [], "note": "验证码: 123456"})
        for key in await manager._redis.keys("*"):
            kind = await manager._redis.type(key)
            value = await manager._redis.lrange(key, 0, -1) if kind == "list" else await manager._redis.get(key)
            assert "Abc!2026" not in str(value) and "13812345678" not in str(value) and "123456" not in str(value)
        await manager.close()
    asyncio.run(run())


def test_field_age_is_preserved_and_old_profile_is_excluded():
    old_at = (datetime.now(timezone.utc) - timedelta(days=181)).timestamp()
    old = MemoryManager._normalize_profile({"background": "旧部门", "work": "GitLab"}, source_request_id="old", updated_at=old_at)
    new = MemoryManager._normalize_profile({"background": "新部门", "work": "GitLab"}, source_request_id="new")
    merged = MemoryManager._merge_profile(old, new)
    assert "新部门" in profile_text(merged) and "旧部门" not in str(merged)
    assert "GitLab" not in profile_text(merged)
    assert merged["field_meta"]["work"]["source_request_id"] == "old"


def test_existing_memory_script_redacts_and_reembeds(db_url):
    store = VectorStore(db_url)
    collection = store.collection("episodic")
    collection.upsert(["old"], ["密码是 Abc!2026"], [{"user_id": "u", "ts": "2026-01-01", "full_text": "手机13812345678"}])
    with read(store.engine) as db:
        before = db.execute(select(vector_documents)).one()
    assert redact_existing_memory(store)["episodic"] == 1
    with read(store.engine) as db:
        after = db.execute(select(vector_documents)).one()
    assert "Abc!2026" not in after.document and "13812345678" not in json.dumps(after.meta)
    assert (before.embedding != after.embedding).any()
    assert redact_existing_memory(store)["episodic"] == 0
    store.dispose()


def test_personal_memory_api_uses_subject_scope_and_audits():
    from evaluation.application import application, auth
    from evaluation.fake_model import scripted_model
    from pydantic_ai.messages import ModelResponse, TextPart
    from api import state

    async def answer(messages, info):
        return ModelResponse(parts=[TextPart("ok")])

    async def run():
        async with application(model=scripted_model(answer)) as (client, _):
            from core.identity import principal_from_token
            alice = principal_from_token(auth()["Authorization"].split()[1])
            bob = principal_from_token(auth("bob")["Authorization"].split()[1])
            await state.memory.store_episodic_turn(alice.scope_key, "c", "a", "VPN故障", "检查账号", {})
            await state.memory.store_episodic_turn(bob.scope_key, "c", "b", "VPN故障", "检查账号", {})
            own = (await client.get("/memory/me", headers=auth())).json()
            foreign = (await client.get("/memory/me", headers=auth("bob"))).json()["episodic"][0]["id"]
            assert len(own["episodic"]) == 1
            assert (await client.delete(f"/memory/me/episodic/{foreign}", headers=auth())).status_code == 404
            response = await client.delete("/memory/me", headers=auth())
            assert response.json()["deleted"] == 1
            assert len((await client.get("/memory/me", headers=auth("bob"))).json()["episodic"]) == 1
            with read(state.memory.engine) as db:
                row = db.execute(select(admin_audit).where(admin_audit.c.event == "memory_deleted")).mappings().one()
            assert row["actor"] == alice.subject and row["tenant"] == alice.tenant
    asyncio.run(run())


def test_working_ablation_keeps_only_platform_version_state(db_url):
    async def run():
        manager = memory(db_url)
        manager.context_mode = "working"
        await manager.commit_turn("u", "c", "pinned", "VPN", {
            "request_id": "pinned", "response": "请检查", "status": "success"},
            state={"agent_spec": "it_copilot", "agent_version": "1.1.0", "entities": {"system": ["VPN"]}})
        context = await manager.get_context("u", "c", "VPN")
        assert context.state == {"agent_spec": "it_copilot", "agent_version": "1.1.0"}
        await manager.close()
    asyncio.run(run())
