from servicedesk_agent.agent.memory.recall import budget_texts
import asyncio

from servicedesk_agent.agent.conversation_state import merge_state
from servicedesk_agent.agent.intent.entities import extract_entities
from servicedesk_agent.agent.intent.recognizer import IntentRecognizer
from servicedesk_agent.agent.intent.taxonomy import IntentCategory, UrgencyLevel
from tests.support.evaluator import load_intent_cases


def test_state_tracks_entities_and_missing_slots():
    state = merge_state({}, "access", {"system": ["GitLab"]}, "申请权限")
    assert state["schema_version"] == 3
    assert state["entities"]["system"] == ["GitLab"]
    assert "employee_id" in state["missing_fields"]
    assert state["turn_count"] == 1


def test_memory_budget():
    values = budget_texts(["a" * 300, "b" * 300, "c" * 300], 128)
    assert len(values) == 1


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
    from servicedesk_agent.agent.conversation_state import known_entities
    state = {"entities": {"employee_id": ["E1001"], "system": ["Jira"]}}
    assert known_entities(state, {"system": ["GitLab"], "device_id": []}) == {"employee_id": ["E1001"], "system": ["GitLab"]}


def test_hard_intent_suite_labels_are_valid():
    from tests.support.reporting import DATASETS_DIR
    cases = load_intent_cases(str(DATASETS_DIR / "intent_v3.json"))
    valid = {item.value for item in IntentCategory}
    assert len(cases) >= 150
    assert len({case.id for case in cases}) == len(cases)
    assert {case.expected_intent for case in cases} == valid
    for case in cases:
        assert case.split in {"calibration", "test"} and case.tags
        assert set(case.acceptable_intents) <= valid and case.expected_intent not in case.acceptable_intents


def test_intent_evaluator_credits_acceptable_intents():
    from servicedesk_agent.agent.intent.recognizer import IntentResult
    from tests.support.evaluator import IntentEvaluator, IntentTestCase

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


def test_bare_employee_and_ticket_ids_are_extracted():
    entities = extract_entities("工号E10031，查一下 INC-1001 和 inc20240512")
    assert entities["employee_id"] == ["E10031"] and entities["ticket_id"] == ["INC-1001", "INC20240512"]
