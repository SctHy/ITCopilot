from copy import deepcopy
import json

import pytest

from servicedesk_agent.cmd import eval as cli
from servicedesk_agent.eval import p1
from servicedesk_agent.eval.diff import compare_ledgers
from servicedesk_agent.eval.ledger import Experiment, InvariantCheck, Report, EnvMetadata
from servicedesk_agent.eval.stats import arm, compare


@pytest.fixture
def ledger():
    exp = Experiment("sample", {"unsafe": "<script>alert(1)</script>"})
    exp.arms = [
        arm("before", {"asr": [0], "f1": [0.8], "p95_ms": [10]}),
        arm("after", {"asr": [0], "f1": [1], "p95_ms": [12]}),
    ]
    for key in exp.arms[0].metrics:
        compare(exp, *exp.arms, key, "latency trade-off")
    exp.checks.append(InvariantCheck("leaks", 0, 0))
    return Report(EnvMetadata.capture("quick", 1, []), [exp]).serialize()


def test_all_filter(tmp_path, monkeypatch):
    calls = []

    async def suite(selected, profile, repeats, url):
        calls.append((selected, repeats, url))
        return {"env": {"datasets": selected}, "experiments": selected, "passed": True}

    monkeypatch.setattr(cli, "evaluate", suite)
    monkeypatch.setattr(p1, "evaluate", suite)
    path = tmp_path / "merged"
    assert (
        cli.main(
            [
                "all",
                "--profile",
                "full",
                "--out",
                str(path),
                "--database-url",
                "pg",
                "--only",
                f"{p1.NAMES[-1]},{cli.NAMES[1]}",
            ]
        )
        == 0
    )
    assert calls == [([cli.NAMES[1]], 5, "pg"), ([p1.NAMES[-1]], 5, None)]
    result = json.loads(path.read_text())
    assert result["env"]["datasets"] == result["experiments"] == [cli.NAMES[1], p1.NAMES[-1]]


@pytest.mark.parametrize(
    "key,after,status",
    [
        ("p95_ms", 11.5, "NEUTRAL"),
        ("p95_ms", 12, "REGRESSED"),
        ("f1", 0.6, "REGRESSED"),
        ("f1", 1, "IMPROVED"),
        ("asr", 1e-12, "REGRESSED"),
    ],
)
def test_diff(ledger, key, after, status):
    other = deepcopy(ledger)
    other["experiments"][0]["arms"][0]["metrics"][key]["median"] = after
    result = compare_ledgers(ledger, other)
    row = next(r for r in result["rows"] if r["arm"] == "before" and r["metric"] == key)
    assert row["status"] == status
    assert result["passed"] == (status != "REGRESSED")


def test_missing(ledger):
    for candidate in (dict(experiments=[]), deepcopy(ledger)):
        if candidate["experiments"]:
            candidate["experiments"][0]["arms"].pop()
        assert not compare_ledgers(ledger, candidate)["passed"]
        assert compare_ledgers(ledger, candidate, allow_missing=True)["passed"]


def test_cli_artifacts(tmp_path, ledger):
    base, candidate, md, html, output = (
        tmp_path / name for name in ["base", "candidate", "md", "html", "diff"]
    )
    base.write_text(json.dumps(ledger))
    ledger["experiments"][0]["passed"] = ledger["passed"] = False
    candidate.write_text(json.dumps(ledger))
    assert (
        cli.main(
            [
                "diff",
                "--baseline",
                str(base),
                "--candidate",
                str(candidate),
                "--out",
                str(output),
                "--json",
            ]
        )
        == 1
    )
    assert json.loads(output.read_text())["rows"][0]["status"] == "INVARIANT_FAIL"
    assert (
        cli.main(
            [
                "report",
                "--in",
                str(candidate),
                "--baseline",
                str(base),
                "--md",
                str(md),
                "--html",
                str(html),
            ]
        )
        == 0
    )
    for path in md, html:
        report = path.read_text()
        assert all(s in report for s in ("Scorecard", "95% CI", "回归 diff", "INVARIANT_FAIL"))
        assert "<script>" not in report
    assert "<svg" in html.read_text()
