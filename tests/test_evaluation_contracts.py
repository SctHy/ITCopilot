import json

from core.intent_calibration import load_temperatures
from core.conversation_state import merge_state
from core.planner import build_plan, is_complex_request
from core.intent_recognizer import IntentRecognizer


def test_calibration_never_crosses_model_or_fallback_boundaries(tmp_path):
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({"temperatures": {"network": 0.25}}))
    assert load_temperatures(path, source="fusion|anthropic:live|local-ngram") == {}
    path.write_text(
        json.dumps({"temperatures_by_source": {"llm|anthropic:live|none": {"network": 0.8}}})
    )
    assert load_temperatures(path, source="llm|anthropic:live|none") == {"network": 0.8}
    assert load_temperatures(path, source="llm|fallback|none") == {}


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
    from core.intent_recognizer import IntentCategory, _fallback_vote

    predicted, _, scores = IntentRecognizer.select_vote(
        "llm",
        _fallback_vote("fallback_exception"),
        {},
        {"intent": IntentCategory.VPN_ISSUE, "confidence": 0.65},
    )
    assert predicted == IntentCategory.VPN_ISSUE and "pattern_fallback" in scores
