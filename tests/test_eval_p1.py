"""Frozen synthetic corpora, production boundaries, and independent metric oracles."""

import asyncio
from collections import Counter
import json
import re

from servicedesk_agent.agent.masking.local_pii import Span
from servicedesk_agent.eval.p1 import DATASETS, FILES, NAMES
from servicedesk_agent.eval.p1_context import LABELS as CONTEXT, dialog, prefix
from servicedesk_agent.eval.p1_guard import guardrails
from servicedesk_agent.eval.p1_mask import masking
from servicedesk_agent.eval.p1_router import LABELS as ROUTES, router_round


def cases(i):
    data = json.loads((DATASETS / FILES[NAMES[i]]).read_text())
    assert "合成" in data["metadata"]
    return data["cases"]


def test_guard_cases_and_production_replay():
    rows = cases(0)
    groups = Counter(r["vector"] for r in rows)
    assert len(groups) == 5 and min(groups.values()) >= 12
    assert len({r["id"] for r in rows}) == len(rows)
    exp = asyncio.run(guardrails(rows, 1))
    assert exp.passed
    base, prod = exp.arms
    assert base.metrics["asr"].mean == 1
    assert prod.metrics["state_detection"].mean == 1
    assert all(
        prod.metrics[k].mean == 0
        for k in ("asr", "unauthorized_write_rate", "cross_tenant_leak", "pii_leak_rate")
    )


def test_clean_chinese_and_synthetic_entities():
    rows = cases(1)
    clean = [r["text"] for r in rows if not r["entities"]]
    assert len(set(clean)) >= 1000
    for term in ("v2.14.3", "INC-20261123", "VPN-691", "HTTP 502", ":8443", "08:30", "部门"):
        assert any(term in t for t in clean)
    positive = [r for r in rows if r["entities"]]
    assert len(positive) >= 200
    patterns = dict(
        ID=r"99000020000101\d{3}[\dX]",
        PHONE=r"13800138\d{3}",
        SECRET=r"FAKE-TEST-ONLY-\d+",
        ADDRESS=r"虚构市星云区样例路\d+号",
        PERSON=r"林星舟|沈云帆|周小禾",
        EMAIL=r"fixture\d+@example\.com",
    )
    kinds = set()
    for row in positive:
        for span in row["entities"]:
            a, b, k = span["start"], span["end"], span["type"]
            assert 0 <= a < b <= len(row["text"])
            assert re.fullmatch(patterns[k], row["text"][a:b])
            kinds.add(k)
    assert kinds == patterns.keys()


def test_masking_arms_and_false_positive_accounting():
    rows = [
        dict(text="核对模型", entities=[]),
        dict(text="联系小蓝", entities=[dict(start=2, end=4, type="PERSON")]),
        dict(text="test@example.com", entities=[dict(start=0, end=16, type="EMAIL")]),
    ]

    class Fake:
        def detect(self, text, **kwargs):
            return (Span(2, 4, "PERSON", 1),) if len(text) == 4 else ()

    exp = asyncio.run(masking(rows, 1, Fake()))
    assert exp.passed
    regex, model, hybrid = exp.arms
    assert regex.metrics["clean_zh_fpr"].mean == 0
    assert model.metrics["coverage"].mean == 0.5
    assert hybrid.metrics["coverage"].mean == 1
    assert hybrid.metrics["clean_replaced_chars"].mean == 2
    assert hybrid.metrics["tool_restore_rate"].mean == 1


def test_long_dialog_and_cost_accounting(tmp_path):
    rows = cases(2)
    assert len(rows) > 1 and all(24 <= len(r["turns"]) <= 30 for r in rows)
    assert all(all(a in str(r["turns"][:2]) for a in r["anchors"]) for r in rows)
    assert prefix([1, 2, 3], [1, 2, 4]) == 2 and prefix([], []) == 0
    observed = {}
    for label in CONTEXT:
        metrics, errors = asyncio.run(
            dialog(label, rows[0], lambda text: list(map(ord, text)), tmp_path)
        )
        assert not any(errors.values())
        assert 0 < metrics["prefix_hit_rate"] < 1
        observed[label] = metrics
    assert observed[CONTEXT[-1]]["summary_input_tokens"] == 0
    assert observed[CONTEXT[-1]]["prefix_hit_rate"] > observed[CONTEXT[-2]]["prefix_hit_rate"]
    assert observed[CONTEXT[-2]]["input_tokens"] < observed[CONTEXT[0]]["input_tokens"]
    assert observed[CONTEXT[-2]]["anchor_retention"] < 1


def test_failover_calls_and_compliance(tmp_path):
    for label, want_available, want_primary in zip(ROUTES, (0, 1, 1), (8, 8, 5)):
        metrics, violations = asyncio.run(router_round(label, tmp_path, 8))
        assert not any(violations.values())
        assert metrics["availability"] == want_available
        assert metrics["failed_primary_calls"] == want_primary
