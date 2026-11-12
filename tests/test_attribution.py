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


def test_cli_reads_durable_rows_and_snapshots_with_tenant_filter(db_url):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    from sqlalchemy import insert
    from core.db import badcases, change_events, shared_engine, turn_records
    from core.tenant_context import system_scope
    engine = shared_engine(db_url)
    with system_scope(), engine.begin() as db:
        for i, row in enumerate(rows(3, 0, start=1000) + rows(3, 2, start=4000)):
            record = {**row, "agent_spec": "it_copilot", "owner": "owner", "conv_id": "conv",
                      "outcome": row["status"], "intent": "other", "intent_confidence": 1.,
                      "route": "agent_read", "build_sha": "test", "output_tokens": 0,
                      "latency_ms": 1., "stage_ms": {}}
            db.execute(insert(turn_records).values(**record))
        db.execute(insert(change_events).values(at=3500, tenant=None, kind="model_route", ref="new",
                                               detail={}, actor="test"))
        db.execute(insert(badcases).values(id="snapshot", request_id="X-read-4000-0", tenant="default",
            source="signal", signals=["tool_error"], status="new", snapshot={"snapshot_partial": True},
            label={}, labeler=None, labeled_at=None, second_label=None, dataset_case_id=None))
    root = Path(__file__).resolve().parents[1]
    run = subprocess.run([sys.executable, str(root / "scripts/attribute_success_drop.py"),
        "--baseline", "1970-01-01T00:00+00:00/1970-01-01T00:50+00:00",
        "--current", "1970-01-01T00:50+00:00/1970-01-01T02:00+00:00", "--tenant", "default"],
        cwd=root, env={**os.environ, "IT_COPILOT_DATABASE_URL": db_url}, text=True, capture_output=True)
    assert run.returncode == 0, run.stderr
    path = Path(run.stdout.rsplit("JSON: ", 1)[1].strip())
    try:
        report = json.loads(path.read_text())
        assert report["baseline"]["n"] == report["current"]["n"] == 3
        assert report["timeline"][0]["event"]["kind"] == "model_route"
        assert any(sample["badcase_id"] == "snapshot" for group in report["samples"] for sample in group["requests"])
    finally:
        path.unlink()
