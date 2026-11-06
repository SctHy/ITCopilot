"""意图规则表、编号抽取、Jev 槽位阈值与缓存计数。"""
import asyncio

import pytest
from prometheus_client import REGISTRY

from agents.intent.entities import extract_entities
from agents.intent.recognizer import IntentRecognizer
from agents.intent.taxonomy import (
    CATEGORY_GUIDE,
    INTENT_CRITERIA,
    INTENT_RULES,
    JEV_INTENT_INSTRUCTIONS,
    IntentCategory,
    jev_questions,
    jev_state,
)
from core.jev import JevResponse
from evaluation.reporting import billable_tokens


def test_one_rule_table_drives_both_llm_guide_and_jev_questions():
    assert set(INTENT_CRITERIA) == set(IntentCategory)
    for intent, description in INTENT_CRITERIA.items():
        assert f"- {intent.value}：{description}" in CATEGORY_GUIDE
        assert jev_questions()["intent"]["criteria"][intent.value] == description
    for rule in INTENT_RULES:
        assert rule in CATEGORY_GUIDE and rule in JEV_INTENT_INSTRUCTIONS
    assert set(jev_questions(with_entities=False)) == {"intent", "urgency"}


def test_jev_state_keeps_only_last_four_history_items():
    history = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    state = jev_state("现在呢", history)
    assert "m1" not in state and "m2" in state and state.endswith("用户最新消息：现在呢")
    assert jev_state("你好", None) == "用户最新消息：你好"


@pytest.mark.parametrize("message,employee,ticket", [
    ("工号E10031", ["E10031"], []),
    ("e1001 的单 inc-20240512", [], ["INC-20240512"]),          # 工号只认大写 E
    ("VPN incident report, including logs", [], []),             # 英文单词不是单号
    ("打印机报错 E1003", [], []), ("错误码: E1003", [], []),       # 设备错误码不是工号
    ("IDE1001 不是工号，INC 也不是单号", [], []),
    ("工号: E1001，再查 INC-1001 和 INC-1001", ["E1001"], ["INC-1001"]),
])
def test_bare_ids(message, employee, ticket):
    entities = extract_entities(message)
    assert entities["employee_id"] == employee and entities["ticket_id"] == ticket


class FakeJev:
    def __init__(self, answers):
        self.answers, self.calls = answers, 0

    async def ask(self, state, questions):
        self.calls += 1
        return JevResponse(answers=self.answers)


def answers(system="GitLab", system_p=.9, role="none"):
    return {"intent": {"choice": "permission_request", "probabilities": {"permission_request": .95}},
            "urgency": {"choice": "normal", "probabilities": {"normal": .9}},
            "system": {"choice": system, "probabilities": {system: system_p}},
            "role": {"choice": role, "probabilities": {role: .9}}}


def test_uncertain_slot_answers_are_not_filled():
    recognizer = IntentRecognizer("test", model="test", jev=FakeJev(answers(system_p=.4)))
    result = asyncio.run(recognizer.recognize("申请那个系统的权限，工号 E1001"))
    assert "system" not in result.entities and result.missing_slots == ["system"] and result.clarification_needed


def test_intent_cache_hits_skip_the_model_and_are_counted():
    jev = FakeJev(answers())
    recognizer = IntentRecognizer("test", model="test", jev=jev)
    labels = {"cache": "intent", "result": "hit"}
    before = REGISTRY.get_sample_value("it_copilot_cache_lookups_total", labels) or 0
    first = asyncio.run(recognizer.recognize("申请 GitLab 权限，工号 E1001"))
    second = asyncio.run(recognizer.recognize("申请 GitLab 权限，工号 E1001"))
    assert first is second and jev.calls == 1
    assert REGISTRY.get_sample_value("it_copilot_cache_lookups_total", labels) == before + 1
    asyncio.run(recognizer.recognize("申请 GitLab 权限，工号 E1001", history=[{"role": "user", "content": "之前"}]))
    assert jev.calls == 2  # 历史不同，缓存键不同


def test_billable_tokens_do_not_double_count_cache_reads():
    assert billable_tokens({"input_tokens": 1000, "output_tokens": 50, "cache_read_tokens": 800}) == 1050
    assert billable_tokens(None) == 0 and billable_tokens({}) == 0


def test_low_confidence_jev_answer_is_used_when_llm_also_fails():
    async def failing_llm(message, history):
        return {"intent": IntentCategory.OTHER, "confidence": .5, "entities": {}, "_source": "fallback_exception"}

    low = answers()
    low["intent"] = {"choice": "vpn_issue", "probabilities": {"vpn_issue": .65}}
    recognizer = IntentRecognizer("test", model="test", jev=FakeJev(low))
    recognizer._llm = failing_llm
    result = asyncio.run(recognizer.recognize("那个连不上"))
    assert result.intent == IntentCategory.VPN_ISSUE and result.recognition_source == "jev_low_confidence"
    assert result.source_scores == {"jev": .65} and result.clarification_needed is False


def test_empty_numeric_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("IT_INTENT_JEV_THRESHOLD", "")
    monkeypatch.setenv("IT_INTENT_CLARIFY_THRESHOLD", "abc")
    recognizer = IntentRecognizer("test", model="test")
    assert recognizer._jev_threshold == .7 and recognizer._clarify_threshold == .6
