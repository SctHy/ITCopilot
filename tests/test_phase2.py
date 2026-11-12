import asyncio

from agents.conversation_state import merge_state
from agents.intent.entities import extract_entities
from agents.intent.recognizer import IntentRecognizer
from agents.intent.taxonomy import IntentCategory, UrgencyLevel
from evaluation.evaluator import load_intent_cases
from memory.conversation_memory import MemoryManager


def test_state_tracks_entities_and_missing_slots():
    state = merge_state({}, "access", {"system": ["GitLab"]}, "申请权限")
    assert state["schema_version"] == 3
    assert state["entities"]["system"] == ["GitLab"]
    assert "employee_id" in state["missing_fields"]
    assert state["turn_count"] == 1


def test_profile_normalization_removes_sensitive_values():
    profile = MemoryManager._normalize_profile({"background": "研发部后端", "work": "维护 GitLab；password=bad", "extra": "x" * 500})
    assert profile == {"schema_version": 4, "background": "研发部后端", "work": "[REDACTED]"}


def test_memory_budget_and_profile_merge():
    from memory.conversation_memory import MemoryManager
    values = MemoryManager._budget_texts(["a" * 300, "b" * 300, "c" * 300], 128)
    assert len(values) == 1
    merged = MemoryManager._merge_profile({"background": "研发部", "work": "GitLab"}, {"work": "GitLab 与 Jira"})
    assert merged == {"schema_version": 4, "background": "研发部", "work": "GitLab 与 Jira"}


def test_profile_revision_replaces_outdated_field():
    from memory.conversation_memory import MemoryManager
    merged = MemoryManager._merge_profile(MemoryManager._normalize_profile({"background": "研发部"}), MemoryManager._normalize_profile({"background": "产品部"}))
    assert merged["background"] == "产品部" and "研发" not in str(merged)


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


def test_slot_given_in_earlier_turn_is_not_asked_again():
    turn1 = merge_state({}, "access", {"employee_id": ["E1001"]}, "我的工号是 E1001")
    turn2 = merge_state(turn1, "access", {}, "帮我重置密码", missing_slots=["employee_id"])
    assert turn2["missing_fields"] == [] and turn2["entities"]["employee_id"] == ["E1001"]
    turn3 = merge_state(turn2, "access", {"system": ["GitLab"]}, "申请 GitLab 权限", missing_slots=["employee_id", "system", "请补充具体 IT 问题或上下文"])
    assert turn3["missing_fields"] == ["请补充具体 IT 问题或上下文"]   # 非实体类澄清项保留


def test_agent_receives_accumulated_entities_current_turn_wins():
    from agents.conversation_state import known_entities
    state = {"entities": {"employee_id": ["E1001"], "system": ["Jira"]}}
    assert known_entities(state, {"system": ["GitLab"], "device_id": []}) == {"employee_id": ["E1001"], "system": ["GitLab"]}


def test_hard_intent_suite_labels_are_valid():
    from evaluation.reporting import DATASETS_DIR
    cases = load_intent_cases(str(DATASETS_DIR / "intent_v3.json"))
    valid = {item.value for item in IntentCategory}
    assert len(cases) >= 150
    assert len({case.id for case in cases}) == len(cases)
    assert {case.expected_intent for case in cases} == valid
    for case in cases:
        assert case.split in {"calibration", "test"} and case.tags
        assert set(case.acceptable_intents) <= valid and case.expected_intent not in case.acceptable_intents


def test_intent_evaluator_credits_acceptable_intents():
    from agents.intent.recognizer import IntentResult
    from evaluation.evaluator import IntentEvaluator, IntentTestCase

    class Fixed:
        def reset_metrics(self): pass
        def metrics(self): return {}
        async def recognize(self, message, history=None, previous_intent_group=None):
            return IntentResult(intent=IntentCategory.VPN_ISSUE, confidence=.9, intent_group="network", urgency=UrgencyLevel.HIGH)

    cases = [
        IntentTestCase("VPN 连不上，帮我建单", "ticket_create", acceptable_intents=("vpn_issue",), tags=("multi_intent",), split="test", expected_urgency="high"),
        IntentTestCase("WiFi 连不上", "wifi_issue", tags=("confusable",), split="test"),
    ]
    report = asyncio.run(IntentEvaluator(Fixed()).evaluate(cases))
    assert report["accuracy"] == .5 and report["strict_accuracy"] == 0
    assert report["by_tag"]["multi_intent"]["accuracy"] == 1 and report["by_tag"]["confusable"]["accuracy"] == 0
    assert report["urgency_accuracy"]["accuracy"] == 1 and report["latency_s"]["p50"] >= 0


def test_followup_inheritance_keeps_specific_llm_intent():
    async def run():
        recognizer = IntentRecognizer("test", model="test")
        recognizer._llm = lambda message, history: asyncio.sleep(0, result={"intent": IntentCategory.VPN_ISSUE, "confidence": .85, "entities": {}, "_llm_used": True, "_source": "llm_tool_use"})
        result = await recognizer.recognize("还是不行", previous_intent_group="network")
        assert result.intent == IntentCategory.VPN_ISSUE and not result.inherited_from_previous
    asyncio.run(run())


def _jev_answers(intent, prob, urgency="normal", system="none", role="none"):
    from core.jev import JevResponse
    return JevResponse(model="jev-1.14.0", usage={"input_tokens": 400, "output_tokens": 2}, answers={
        "intent": {"choice": intent, "confidence": prob, "probabilities": {intent: prob}},
        "urgency": {"choice": urgency, "probabilities": {urgency: .9}},
        "system": {"choice": system, "probabilities": {system: .9}},
        "role": {"choice": role, "probabilities": {role: .9}},
    })


class _FakeJev:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        return self.response


def test_recognizer_accepts_confident_jev_and_fills_policy_slots():
    async def run():
        jev = _FakeJev(_jev_answers("permission_request", .95, system="GitLab", role="developer"))
        recognizer = IntentRecognizer("test", model="test", jev=jev)
        async def no_llm(message, history):
            raise AssertionError("confident Jev answers must not call the LLM")
        recognizer._llm = no_llm
        result = await recognizer.recognize("工号 E1001 申请 GitLab developer 权限")
        assert result.intent == IntentCategory.PERMISSION_REQUEST and result.recognition_source == "jev"
        assert result.entities["system"] == ["GitLab"] and result.entities["role"] == ["developer"] and result.entities["employee_id"] == ["E1001"]
        assert not result.clarification_needed and result.source_scores == {"jev": .95}
        assert recognizer.metrics()["jev_accepted"] == 1
    asyncio.run(run())


def test_recognizer_escalates_low_confidence_or_failed_jev_to_llm():
    async def run():
        llm_result = {"intent": IntentCategory.PASSWORD_RESET, "confidence": .9, "urgency": UrgencyLevel.NORMAL, "entities": {}, "reasoning": "", "_llm_used": True, "_source": "llm_tool_use"}
        recognizer = IntentRecognizer("test", model="test", jev=_FakeJev(_jev_answers("account_unlock", .62)))
        recognizer._llm = lambda message, history: asyncio.sleep(0, result=dict(llm_result))
        low = await recognizer.recognize("mima wang le")
        assert low.intent == IntentCategory.PASSWORD_RESET and low.recognition_source == "llm_tool_use"
        assert low.source_scores == {"llm": .9, "jev": .62}
        recognizer._jev = _FakeJev(error=TimeoutError())
        failed = await recognizer.recognize("忘记密码了")
        assert failed.intent == IntentCategory.PASSWORD_RESET
        assert recognizer.metrics()["jev_escalated"] == 1 and recognizer.metrics()["jev_errors"] == 1
    asyncio.run(run())


def test_bare_employee_and_ticket_ids_are_extracted():
    entities = extract_entities("工号E10031，查一下 INC-1001 和 inc20240512")
    assert entities["employee_id"] == ["E10031"] and entities["ticket_id"] == ["INC-1001", "INC20240512"]


def test_jev_path_reads_ids_from_history():
    async def run():
        recognizer = IntentRecognizer("test", model="test", jev=_FakeJev(_jev_answers("ticket_status", .9)))
        result = await recognizer.recognize("这个单大概多久能处理", history=[{"role": "user", "content": "电脑蓝屏"}, {"role": "assistant", "content": "已为你创建工单 INC20240610。"}])
        assert result.entities["ticket_id"] == ["INC20240610"] and not result.clarification_needed
    asyncio.run(run())
