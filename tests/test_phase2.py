import asyncio
from types import SimpleNamespace

from core.conversation_state import merge_state
from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from memory.conversation_memory import MemoryManager
from evaluation.evaluator import load_intent_cases


def test_state_v2_tracks_entities_and_missing_slots():
    state = merge_state({}, "access", {"system": ["GitLab"]}, "申请权限")
    assert state["schema_version"] == 2
    assert state["entities"]["system"] == ["GitLab"]
    assert "employee_id" in state["missing_fields"]
    assert state["turn_count"] == 1


def test_profile_normalization_removes_sensitive_values():
    profile = MemoryManager._normalize_profile({
        "department": "研发",
        "systems": ["GitLab", "password=bad"],
        "entities": {"employee_id": ["u1001"], "token": ["secret-token"]},
    })
    assert profile["department"] == "研发"
    assert "[REDACTED]" in profile["systems"]
    assert profile["entities"]["token"] == ["[REDACTED]"]
    assert profile["schema_version"] == 3
    assert profile["stable_attributes"]["department"] == "研发"


def test_memory_budget_and_profile_merge():
    from memory.conversation_memory import MemoryManager
    values = MemoryManager._budget_texts(["a" * 300, "b" * 300, "c" * 300], 128)
    assert len(values) == 1
    merged = MemoryManager._merge_profile({"systems": ["GitLab"], "entities": {"role": ["engineer"]}}, {"systems": ["Jira"], "entities": {"role": ["engineer", "admin"]}})
    assert merged["systems"] == ["GitLab", "Jira"]
    assert merged["entities"]["role"] == ["engineer", "admin"]


def test_profile_conflict_marks_old_fact_superseded():
    from memory.conversation_memory import MemoryManager
    old = MemoryManager._normalize_profile({"department": "研发"})
    new = MemoryManager._normalize_profile({"department": "产品"})
    merged = MemoryManager._merge_profile(old, new)
    assert any(f["value"] == "研发" and f["status"] == "superseded" for f in merged["facts"])


def test_local_embedding_is_normalized():
    vector = IntentRecognizer._local_embed("VPN 连不上")
    assert len(vector) == 256
    assert abs(sum(x * x for x in vector) - 1.0) < 1e-6


def test_versioned_intent_suite_is_complete():
    cases = load_intent_cases()
    assert len(cases) >= 40
    assert any(case.context and case.context.get("previous_intent_group") == "network" for case in cases)


def test_intent_result_merges_llm_entities():
    async def run():
        recognizer = IntentRecognizer("test", model="test")
        recognizer._embedding_match = lambda message: asyncio.sleep(0, result={"intent": IntentCategory.PERMISSION_REQUEST, "confidence": .9, "source": "local-ngram"})
        recognizer._llm = lambda message, history: asyncio.sleep(0, result={"intent": IntentCategory.PERMISSION_REQUEST, "confidence": .95, "urgency": UrgencyLevel.NORMAL, "entities": {"system": ["GitLab"]}, "reasoning": "access request"})
        result = await recognizer.recognize("申请 GitLab 权限")
        assert result.intent == IntentCategory.PERMISSION_REQUEST
        assert result.entities["system"] == ["GitLab"]
    asyncio.run(run())
