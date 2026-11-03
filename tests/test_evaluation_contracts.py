
from agents.conversation_state import merge_state
from agents.planner import build_plan, is_complex_request
from agents.intent_recognizer import IntentRecognizer


def test_create_ticket_does_not_require_existing_ticket_number():
    state = merge_state({}, "ticket", {"employee_id": ["1001"]}, "创建工单", intent="ticket_create")
    assert state["missing_fields"] == []
    query = merge_state({}, "ticket", {}, "查工单", intent="ticket_status")
    assert query["missing_fields"] == ["ticket_id"]


def test_compound_diagnosis_routes_and_has_no_dangling_dependencies():
    message = "VPN故障，同时电脑蓝屏并安装软件，请分别检查并创建工单"
    assert is_complex_request(message)
    plan = build_plan(message, "p")
    ids = {s.step_id for s in plan.steps}
    assert all(set(s.depends_on) <= ids for s in plan.steps)


def test_llm_failure_remains_labelled_fallback():
    from agents.intent_recognizer import IntentCategory, _fallback_vote

    predicted, _, scores = IntentRecognizer.select(
        _fallback_vote("fallback_exception"),
        {"intent": IntentCategory.VPN_ISSUE, "confidence": 0.65},
    )
    assert predicted == IntentCategory.VPN_ISSUE and "pattern_fallback" in scores
