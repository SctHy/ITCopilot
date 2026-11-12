
from agents.conversation_state import merge_state
from agents.intent.recognizer import IntentRecognizer


def test_create_ticket_does_not_require_existing_ticket_number():
    state = merge_state({}, "ticket", {"employee_id": ["1001"]}, "创建工单", intent="ticket_create")
    assert state["missing_fields"] == []
    query = merge_state({}, "ticket", {}, "查工单", intent="ticket_status")
    assert query["missing_fields"] == ["ticket_id"]


def test_llm_failure_remains_labelled_fallback():
    from agents.intent.recognizer import _fallback_vote
    from agents.intent.taxonomy import IntentCategory

    predicted, _, scores = IntentRecognizer.select(
        _fallback_vote("fallback_exception"),
        {"intent": IntentCategory.VPN_ISSUE, "confidence": 0.65},
    )
    assert predicted == IntentCategory.VPN_ISSUE and "pattern_fallback" in scores


def test_offline_application_isolates_channels_and_all_model_paths(monkeypatch):
    import asyncio
    from pydantic_ai.models.test import TestModel
    from evaluation.application import application

    monkeypatch.setenv("FEISHU_APP_ID", "must-not-start")
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-test-placeholder")
    monkeypatch.setenv("IT_INTENT_BACKEND", "jev")
    monkeypatch.setenv("IT_COPILOT_RERANK_BACKEND", "jev")
    model = TestModel()

    async def run():
        async with application(model=model) as (_, state):
            assert state.channels is None and state.feishu is None
            assert state.orchestrator._intent_recognizer._jev is None
            assert state.tool_manager._jev is None
            assert state.orchestrator._intent_recognizer.llm is model
            assert state.orchestrator.agent._llm is model

    asyncio.run(run())
