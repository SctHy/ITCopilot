"""Magpie-style contextual rules: complete values and nearby negative controls."""
import json

import pytest

from core.local_pii import Span, accept_span
from core.model_masking import MaskingSession, mask_request
from core.pii_rules import chinese_name_spans, iban_spans
from core.redaction import REDACTED, redact_text, sensitive_spans, user_secrets


@pytest.mark.parametrize("text,value", [
    ("密码：蓝天测试口令2030。", "蓝天测试口令2030"),
    ("密碼為『unused』", "unused"),
    ("password='two words!.'", "two words!."),
    ('password="expired"', "expired"),
    ('password="xxxx"', "xxxx"),
    ('password="trailing\\\\"', "trailing\\\\"),
    ("password=BackupKey!349。", "BackupKey!349"),
    ("API Key 是 UQx-Test-913。", "UQx-Test-913"),
    ("PIN码：7518", "7518"), ("pin 为 7518", "7518"),
    ("CVV: 398", "398"), ("安全码 398", "398"),
    ("验证码 862453", "862453"), ("otp 862453", "862453"),
    ("护照号码：E87654321", "E87654321"),
    ("Passport No.: 534761298", "534761298"),
    ("SSN 536-22-1847 on file", "536-22-1847"),
    ("社会安全号码为536-22-1847。", "536-22-1847"),
    ("social security number is 536221847", "536221847"),
    ("ssn: 536 22 1847", "536 22 1847"),
    ("driver license: A98765432", "A98765432"),
    ("联系电话 +44 (0)20 7123 4567", "+44 (0)20 7123 4567"),
    ("Phone: +1-212-555-0198x201", "+1-212-555-0198x201"),
    ("电话 +86 139 7654 3210", "+86 139 7654 3210"),
    ("card 4111 1111 1111 1111", "4111 1111 1111 1111"),
    ("卡号4111111111111111请保密", "4111111111111111"),
    ("电话+44 20 7123 4567请联系我", "+44 20 7123 4567"),
    ("card 3782-822463-10005", "3782-822463-10005"),
    ("IBAN: DE89 3704 0044 0532 0130 00 THEN", "DE89 3704 0044 0532 0130 00"),
    ("BE68 5390 0754 7034 NEXT", "BE68 5390 0754 7034"),
    ("GB82WEST12345698765432", "GB82WEST12345698765432"),
])
def test_explicit_values_are_completely_masked_and_restored(text, value):
    assert any(text[a:b] == value for a, b, _ in sensitive_spans(text))
    session = MaskingSession()
    body = mask_request({"messages": [{"role": "user", "content": text}]}, session)
    masked = body["messages"][0]["content"]
    assert value not in masked
    assert json.loads(session.restore(json.dumps(masked), tool=True)) == text


@pytest.mark.parametrize("text", [
    "password=${DB_PASSWORD}", "password=[REDACTED]", "password=****",
    "token 是 expired", "token 是 3600000 ms", "RSA 密钥为 2048 位",
    "密码长度为 12 个字符", "密码是错误的", "请输入密码：",
    "spin 是 778812", "max_tokens=128000", "token_count=128000",
    "order_id=4111111111111111", "订单号13812345678", "设备号4111111111111111",
    "timestamp: 1612345679345", "20211012113022", "0000000000000000",
    "invalid card 4111111111111112", "X4111111111111111Y",
    "GB82WEST12345698765433", "GB82WEST12345698765432X",
    "SSN 000-22-1847", "SSN 666-22-1847", "SSN 936-22-1847",
    "SSN 536-00-1847", "SSN 536-22-0000", "X536-22-1847Y",
    "passport.js handles auth", "import passport from 'passport'", "passport: ABCDEFG",
    "E87654321", "用户 alice，部门研发", "ssh git@github.com:org/project.git",
])
def test_normal_identifiers_placeholders_and_invalid_checksums_are_preserved(text):
    assert redact_text(text) == text


@pytest.mark.parametrize("value,accepted", [
    ("ALICE MORGAN", True), ("Declan J. Griffiths", True), ("Ludwig van Beethoven", True),
    ("O'Connor", True), ("API", False), ("User yetone", False), ("LESSONS.md", False),
    ("list_threads", False), ("OpenAI", False), ("alice", False),
])
def test_model_person_shape(value, accepted):
    assert accept_span(value, 0, len(value), "PERSON") is accepted


@pytest.mark.parametrize("text,name", [
    ("申请人：顾清和。", "顾清和"), ("我叫王小明今天上班。", "王小明"),
    ("联系人：欧阳晓晴，", "欧阳晓晴"), ("我是夏之遥。", "夏之遥"),
    ("联系人：南宫子墨", "南宫子墨"),
])
def test_chinese_name_requires_context_and_complete_boundary(text, name):
    assert [text[a:b] for a, b, _ in chinese_name_spans(text)] == [name]


@pytest.mark.parametrize("text", ["我是程序员", "客户程序已经更新了", "通知文件已经更新", "联系人字段为空", "王国的军队", "张量计算", "申请人需要补充信息", "联系管理员", "检查文案", "请核对用户名", "不包含个人联系方式。", "联系方式已更新", "联系方向错误", "通知方案已变更"])
def test_chinese_words_not_names(text):
    assert not list(chinese_name_spans(text))


def test_full_model_span_wins_over_contained_numeric_rule():
    text = "32 West Road, call 13876543210"
    session = MaskingSession()
    session.findings = {text: (Span(0, len(text), "ADDRESS", .99),)}
    masked = mask_request({"messages": [{"role": "user", "content": text}]}, session)["messages"][0]["content"]
    assert masked == session.reverse[text]
    assert session.restore(masked) == REDACTED


def test_longer_name_replaces_partial_model_name():
    text = "申请人顾清和的电脑"
    session = MaskingSession()
    session.findings = {text: (Span(3, 5, "PERSON", .99), Span(3, 6, "PERSON", 1.0))}
    masked = mask_request({"messages": [{"role": "user", "content": text}]}, session)["messages"][0]["content"]
    assert "和" not in masked and "的电脑" in masked


def test_assignment_punctuation_does_not_break_known_value_propagation():
    text = "BackupKey!349 appears first; password=BackupKey!349。"
    session = MaskingSession()
    masked = mask_request({"messages": [{"role": "user", "content": text}]}, session)["messages"][0]["content"]
    assert "BackupKey!349" not in masked and masked.endswith("。")


def test_iban_does_not_consume_following_uppercase_word():
    text = "BE68 5390 0754 7034 THEN CONTINUE"
    spans = list(iban_spans(text))
    assert spans == [(0, 19, "ACCOUNT")]


@pytest.mark.parametrize("value", ["EMP123456", "CUST98765432", "A-789012-Z", "C12-3456-789", "F481516", "my_user99"])
def test_model_alphanumeric_business_ids_are_not_accounts(value):
    for prefix in ("employee ", "customerId: ", "", "account: "):
        text = prefix + value
        assert not accept_span(text, len(prefix), len(text), "ACCOUNT")


def test_model_validated_iban_is_an_exception_not_arbitrary_alphanumeric_ids():
    value = "GB82 WEST 1234 5698 7654 32"
    assert accept_span(value, 0, len(value), "ACCOUNT")
    assert not accept_span(value[:-1] + "3", 0, len(value), "ACCOUNT")


def test_reply_scrubbing_uses_the_same_unicode_secret_values():
    assert user_secrets("密码：蓝天测试口令2030。PIN码：7518") == {"蓝天测试口令2030", "7518"}
    assert user_secrets("RSA 密钥为 2048 位") == set()
