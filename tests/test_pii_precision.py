"""Protection/precision pairs for contextual identifiers and model post-filtering."""
import json

import pytest

from servicedesk_agent.agent.masking.local_pii import accept_span
from servicedesk_agent.agent.masking.model_masking import MaskingSession, mask_request
from servicedesk_agent.agent.masking.redaction import redact_text, sensitive_spans


@pytest.mark.parametrize("text,value,kind", [
    ("车牌：粤B12345", "粤B12345", "ID"),
    ("沪AD12345 请登记", "沪AD12345", "ID"),
    ("牌照是京AABCDE", "京AABCDE", "ID"),
    ("粤B12345的车", "粤B12345", "ID"),
    ("手机号 138 1234 5678", "138 1234 5678", "PHONE"),
    ("联系 +86 138-1234-5678", "+86 138-1234-5678", "PHONE"),
    ("电话0086 13812345678", "0086 13812345678", "PHONE"),
    ("证件：110105 19491231 002X", "110105 19491231 002X", "ID"),
    ("110105-19491231-002x", "110105-19491231-002x", "ID"),
    ("出入境证件号E87654321", "E87654321", "ID"),
    ("他的护照发给你了 G87654321", "G87654321", "ID"),
    ("卡号：4111111111111112", "4111111111111112", "ACCOUNT"),
    ("银行卡4111 1111 1111 1112", "4111 1111 1111 1112", "ACCOUNT"),
    ("Credit card number: 4111-1111-1111-1112", "4111-1111-1111-1112", "ACCOUNT"),
    ("转账到张小明的卡 6225887657041209", "6225887657041209", "ACCOUNT"),
    ("打钱到这个卡6226302325727343", "6226302325727343", "ACCOUNT"),
    ("6221555442867966 张小明的银行卡", "6221555442867966", "ACCOUNT"),
    ("6221555442867966 银行卡", "6221555442867966", "ACCOUNT"),
    ("身份证440105200410270579 银行卡6225687295458581", "440105200410270579", "ID"),
    ("身份证510108198002073258 银行卡号：6226893431719166", "510108198002073258", "ID"),
])
def test_complete_identifiers_and_exact_tool_restoration(text, value, kind):
    assert (text.index(value), text.index(value)+len(value), kind) in sensitive_spans(text)
    session = MaskingSession()
    masked = mask_request({"messages": [{"role": "user", "content": text}]}, session)["messages"][0]["content"]
    assert value not in masked
    assert json.loads(session.restore(json.dumps(masked), tool=True)) == text


@pytest.mark.parametrize("text", [
    "京AORDER", "code_粤B12345", "粤B12345.txt", "粤B123456789",
    "employee_id: 13812345678", "customer ID: 4111111111111111",
    "设备编号：138 1234 5678", "order_id=4111111111111111",
    "设备卡号：4111111111111111", "工号：粤B12345",
    "110105 19491231 0021", "X110105 19491231 002X",
    "invalid card 4111111111111112", "4111111111111112",
    "银行卡号：0000000000000000", "卡号长度为16", "卡号：${CARD_NUMBER}",
    "卡号：41111111111111123456789", "卡号：4111 1111 1111 1112 1234",
    "护照模块版本 E20261114", "E87654321", "护照已提交；订单号E87654321",
    "订单号6221555442867966 银行卡已付款", "设备卡号6221555442867966",
    "转账成功；卡槽设备6221555442867966", "6221555442867966 是库存计数，银行卡待绑定",
    "4444 4111 1111 1111 1112 银行卡",
])
def test_non_pii_and_incomplete_prefixes_not_masked(text):
    assert redact_text(text) == text


@pytest.mark.parametrize("value", [
    "Kane County, IL, USA", "Colorado Springs, 80919", "05067, USA", "VT",
    "Edmonton, Alberta, Canada T7C 4A1", "27.7689 N, 82.2536 W", "北京市朝阳区",
    "St. Louis, MO", "Hartford, CT", "Amsterdam 1234 AB", "Madrid 28005",
    "No.45", "120 90210", "120 90210 CA", "No.45.12",
])
def test_coarse_geography_is_not_a_private_address(value):
    assert not accept_span(value, 0, len(value), "ADDRESS")


@pytest.mark.parametrize("value", [
    "123 Main Street, Seattle, WA", "12 Linden Gardens", "Main Street",
    "P.O. Box 421", "Via Roma 12", "Hauptstraße 15", "北京市朝阳区建国路20号",
    "5/12 Queen Road", "Baker St.", "Hammarby Allé 87", "Elisebaan 493",
    "Keizersgracht 123, 1015 CW Amsterdam", "Jann-Söding-Allee 104",
    "C/ Mayor, 123 - 28005 Madrid", "Lange Voorhout 16", "Courtyard, 71",
    "Recipient Name\n707 Jennifer Skyway, Studio 17H",
    "(566 Blair Vista", "35, Pall Mall, London, UK",
    "239 47 Ave", "20 201st SW", "312 25 1/2 Ave S B-03", "157 29 St N",
    "Jalan Sudirman No.45", "İstiklal Caddesi No:120", "27 26 So Zaragoza",
])
def test_street_addresses_remain_protected(value):
    assert accept_span(value, 0, len(value), "ADDRESS")


@pytest.mark.parametrize("prefix,value,kind", [
    ("employee id: ", "23-83214", "ACCOUNT"),
    ("customer ID: ", "592813764", "ACCOUNT"),
    ("device identifier, ", "490154203237518", "ACCOUNT"),
    ('**customer_id**: "', "13812345678", "PHONE"),
    ("What is your race/ethnicity?\n- ", "Jebali", "PERSON"),
    ("identifies as ", "White", "PERSON"),
])
def test_explicit_non_pii_field_blocks_model_guess(prefix, value, kind):
    text = prefix + value
    assert not accept_span(text, len(prefix), len(text), kind)


def test_white_is_not_blacklisted_as_a_surname():
    assert accept_span("Contact: Alice White", 9, 20, "PERSON")


def test_business_field_comma_does_not_suppress_a_person_in_prose():
    text = "Thank you for providing your order number, Edoardo."
    start = text.index("Edoardo")
    assert accept_span(text, start, start + len("Edoardo"), "PERSON")
    field = 'employee_id: "Edoardo"'
    start = field.index("Edoardo")
    assert not accept_span(field, start, start + len("Edoardo"), "PERSON")
