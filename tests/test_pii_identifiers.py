"""Contextual PII and recognizable secrets: values, boundaries and counterexamples."""
import json

import pytest

from it_copilot.agent.masking.model_masking import MaskingSession, mask_request
from it_copilot.agent.masking.redaction import redact_text, sensitive_spans, user_secrets


@pytest.mark.parametrize("label,value,kind", [
    ("Bank Account", "AB9817654321", "ACCOUNT"),
    ("Account No.", "H9817654321", "ACCOUNT"),
    ("account_number", "9817654321", "ACCOUNT"),
    ("Bank Routing Number", "021000021", "ACCOUNT"),
    ("Routing No.", "021000021", "ACCOUNT"),
    ("BBAN", "812345678901234567", "ACCOUNT"),
    ("Medical Record Number", "MRN-968432", "ACCOUNT"),
    ("medical_record_number", "MED96843217", "ACCOUNT"),
    ("MRN", "MED96843217", "ACCOUNT"),
    ("Health Plan Beneficiary Number", "HPB-96843217", "ACCOUNT"),
    ("银行账号", "812345678901", "ACCOUNT"),
    ("病历号", "BL96843217", "ACCOUNT"),
    ("Tax Identification Number", "68-7419352", "ID"),
    ("National ID", "ID96843217", "ID"),
    ("License Plate", "ABC96843", "ID"),
    ("VIN", "1HGCM82633A004352", "ID"),
])
@pytest.mark.parametrize("template", ['{label}: {value}.', '"{label}": "{value}"', '**{label}:** {value}, next'])
def test_named_identifiers_mask_whole_value_and_restore(label, value, kind, template):
    text = template.format(label=label, value=value)
    assert (text.index(value), text.index(value) + len(value), kind) in sensitive_spans(text)
    session = MaskingSession()
    masked = mask_request({"messages": [{"role": "user", "content": text}]}, session)["messages"][0]["content"]
    assert value not in masked
    assert json.loads(session.restore(json.dumps(masked), tool=True)) == text


@pytest.mark.parametrize("text", [
    "employee_id: EMP96843217", "customer_id: CUST96843217", "Shipment ID: MRN-968432",
    "MRN-968432", "bank_account_count=100000", "tax_id_length=100000",
    "account: CUST96843217", "bank account: required", "Bank Account: 0000000000",
    "national_id: ${NATIONAL_ID}", "MRN: <redacted>", "mrn: 123456 bytes",
    "bank account: abc.def123456", "MRN: ABC12345_678", "MRN: ABC12345/678",
    "MRN: ABC12345.ext", "tax_identifier: ABC12345", "VIN: README",
    "key: ordinary-map-key", "public_key: documentation", "API_KEY: ${SERVICE_KEY}",
    "verification_code_length: 123456", "authentication code is 123456 seconds",
    "verification code is sent to your phone", "authentication code is optional",
    "authentication code is disabled", "verification code is delivered separately",
    "npm_abc", "hf_example", "xoxb-short", "ASIA1234", "sk_test_example",
    "v1.0.1234", "123e4567-e89b-12d3-a456-426614174000",
])
def test_business_identifiers_and_nonvalues_are_not_pii(text):
    assert redact_text(text) == text


@pytest.mark.parametrize("value", [
    "AIza" + "Ab9_" * 8 + "Xy7",
    "xoxb-" + "1234567890-0987654321-AbCdE",
    "sk_live_" + "aB9" * 8,
    "ASIA" + "A1" * 8,
    "hf_" + "Ab9" * 10,
    "gsk_" + "Ab9" * 14,
    "xai-" + "Ab9" * 14,
    "npm_" + "Ab9" * 12,
    "pypi-" + "Ab9_" * 13,
    "dop_v1_" + "a9" * 32,
    "SG." + "Ab9_" * 5 + "XY" + "." + "Xy7_" * 10 + "abc",
])
def test_provider_keys_without_assignment_are_detected_and_reply_scrubbed(value):
    text = "The credential leaked: " + value + "."
    assert redact_text(text) == "The credential leaked: [REDACTED]."
    assert value in user_secrets(text)
    assert redact_text("prefix_" + value) == "prefix_" + value


@pytest.mark.parametrize("text,value", [
    ("Two-factor authentication code is 859341.", "859341"),
    ("verification_code: 859341", "859341"),
    ("Pre-Shared Key: 'local-test-only!83'", "local-test-only!83"),
    ("access_key=LocalTestOnly!83", "LocalTestOnly!83"),
    ("card 2223 0000 4840 0011", "2223 0000 4840 0011"),
])
def test_additional_secrets_and_card_prefix(text, value):
    assert value not in redact_text(text)
