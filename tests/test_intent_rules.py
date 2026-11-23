"""意图规则表、编号抽取与分类阈值。"""

import pytest

from it_copilot.agent.intent.entities import extract_entities
from it_copilot.agent.intent.recognizer import IntentRecognizer
from it_copilot.agent.intent.taxonomy import (
    CATEGORY_GUIDE,
    INTENT_CRITERIA,
    INTENT_RULES,
    IntentCategory,
)
from tests.support.reporting import billable_tokens


def test_llm_guide_covers_all_intents_and_rules():
    assert set(INTENT_CRITERIA) == set(IntentCategory)
    for intent, description in INTENT_CRITERIA.items():
        assert f"- {intent.value}：{description}" in CATEGORY_GUIDE
    for rule in INTENT_RULES:
        assert rule in CATEGORY_GUIDE


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


def test_billable_tokens_do_not_double_count_cache_reads():
    assert billable_tokens({"input_tokens": 1000, "output_tokens": 50, "cache_read_tokens": 800}) == 1050
    assert billable_tokens(None) == 0 and billable_tokens({}) == 0


def test_empty_numeric_env_values_fall_back_to_defaults(monkeypatch):
    monkeypatch.setenv("IT_INTENT_CLARIFY_THRESHOLD", "abc")
    recognizer = IntentRecognizer("test", model="test")
    assert recognizer._clarify_threshold == .6
