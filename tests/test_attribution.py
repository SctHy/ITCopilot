from evaluation.attribution import attribute, markdown, slices, wilson


def rows(n, failures, *, model="X", group="read", start=0):
    return [dict(request_id=f"{model}-{group}-{start}-{i}", created_at=start + i,
                 status="failed" if i < failures else "success", error_code=None, signals=[],
                 model=model, intent_group=group, agent_version="v1", route_reason="primary",
                 intent_source="jev", channel="web", tenant="default", policy_version="p1",
                 kb_version="k1", skill_fp="s1", prompt_fp="p1", tools_fp="t1",
                 model_requests=2, input_tokens=1000, cache_read_tokens=500, tools=[])
            for i in range(n)]


def test_rate_drop_ranks_model_and_aligns_change():
    before = rows(200, 0, start=1000) + rows(200, 0, model="Y", start=1000)
    after = rows(200, 160, start=4000) + rows(200, 0, model="Y", start=4000)
    report = attribute(before, after, [{"at": 3500, "kind": "model_route", "ref": "changed"}])
    assert report["top_slices"][0]["dimension"] == "model"
    assert report["top_slices"][0]["value"] == "X"
    assert report["diagnosis"] == "rate"
    assert report["timeline"][0]["significant"]
    assert report["timeline"][0]["failure_increased"]
    assert len(report["samples"][0]["requests"]) == 5
    assert "model_route" in markdown(report)


def test_pure_traffic_mix_has_no_rate_contribution():
    before = rows(100, 10, group="read") + rows(100, 90, group="write")
    after = rows(100, 10, group="read") + rows(900, 810, group="write")
    report = attribute(before, after, [])
    assert report["diagnosis"] == "mix"
    assert report["contributions"]["intent_group"]["rate"] == 0
    assert abs(report["contributions"]["intent_group"]["mix"] - .32) < 1e-9


def test_empty_windows_are_unknown_and_new_slice_is_not_rate_regression():
    assert wilson(0, 0) is None
    assert attribute([], [], [])["baseline"]["success_rate"] is None
    result = slices(rows(10, 0, model="X"), rows(10, 8, model="Y"), "model")
    assert all(row["rate"] == 0 for row in result)
    assert not any(row["comparable"] for row in result)


def test_tool_rates_use_tool_calls_as_denominator():
    before, after = rows(10, 0), rows(10, 0, start=4000)
    before[0]["tools"] = [{"name": "search", "error_class": "success"}] * 9 + [{"name": "search", "error_class": "timeout"}]
    after[0]["tools"] = [{"name": "search", "error_class": "success"}, {"name": "search", "error_class": "timeout"}]
    report = attribute(before, after, [])
    assert report["tool_errors"][0]["delta"] == .4
