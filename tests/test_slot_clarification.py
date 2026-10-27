import asyncio

from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel


def _recognizer(intent: IntentCategory):
    recognizer = IntentRecognizer("test", model="test")
    recognizer._embedding_match = lambda message: asyncio.sleep(
        0, result={"intent": intent, "confidence": 0.95, "source": "test"}
    )
    recognizer._llm = lambda message, history: asyncio.sleep(
        0,
        result={
            "intent": intent,
            "confidence": 0.95,
            "urgency": UrgencyLevel.NORMAL,
            "entities": {},
            "reasoning": "test",
            "_llm_used": False,
            "_source": "test",
        },
    )
    return recognizer


def test_password_reset_without_employee_id_asks_for_slot():
    async def run():
        recognizer = _recognizer(IntentCategory.PASSWORD_RESET)
        result = await recognizer.recognize("帮我重置密码")
        assert result.intent == IntentCategory.PASSWORD_RESET
        assert result.clarification_needed is True
        assert result.missing_slots == ["employee_id"]
        assert result.entities["employee_id"] == []

    asyncio.run(run())


def test_password_reset_with_employee_id_extracts_slot():
    async def run():
        recognizer = _recognizer(IntentCategory.PASSWORD_RESET)
        result = await recognizer.recognize("帮我重置密码，我的工号是 1001")
        assert result.intent == IntentCategory.PASSWORD_RESET
        assert result.entities["employee_id"] == ["1001"]
        assert result.clarification_needed is False
        assert result.missing_slots == []

    asyncio.run(run())


def test_account_unlock_without_employee_id_asks_for_slot():
    async def run():
        recognizer = _recognizer(IntentCategory.ACCOUNT_UNLOCK)
        result = await recognizer.recognize("我的账号被锁了，帮我解锁账号")
        assert result.intent == IntentCategory.ACCOUNT_UNLOCK
        assert result.clarification_needed is True
        assert result.missing_slots == ["employee_id"]

    asyncio.run(run())


def test_account_unlock_with_employee_id_extracts_slot():
    async def run():
        recognizer = _recognizer(IntentCategory.ACCOUNT_UNLOCK)
        result = await recognizer.recognize("帮我解锁账号，员工编号 8866")
        assert result.intent == IntentCategory.ACCOUNT_UNLOCK
        assert result.entities["employee_id"] == ["8866"]
        assert result.clarification_needed is False

    asyncio.run(run())


def test_employee_id_pattern_supports_colon_copula_and_chinese_labels():
    recognizer = IntentRecognizer("test", model="test")
    for message, expected in (
        ("工号:1001", ["1001"]),
        ("工号：1001", ["1001"]),
        ("工号是 1001", ["1001"]),
        ("工号为 1001", ["1001"]),
        ("员工编号 8866", ["8866"]),
    ):
        entities = recognizer._extract_entities(message)
        assert entities["employee_id"] == expected, message
