import asyncio
from collections import Counter
import json

import numpy as np
import pytest

from servicedesk_agent.cmd.eval import main
from servicedesk_agent.eval import rag, stats
from servicedesk_agent.eval.ledger import DATA, ROOT, EnvMetadata, Experiment, InvariantCheck, Report
from servicedesk_agent.eval.workloads import fair, fault, notify
from servicedesk_agent.workflow.store import WorkflowStore


def test_stats_and_report():
    assert stats.percentile([1, 2, 3, 4], 50) == 2
    assert stats.summary([1, 2, 3, 4]).median == 2.5
    assert stats.bootstrap([1, 2, 3], [2, 4, 6]) == (100, 100)
    assert stats.bootstrap([1, 2], [3, 4, 5], paired=False) == stats.bootstrap(
        [1, 2], [3, 4, 5], paired=False
    )
    assert stats.bootstrap([0], [1]) == (None, None)
    with pytest.raises(ValueError):
        stats.bootstrap([1], [1, 2])
    hits = ["a", "a", "b"]
    assert stats.recall_at_k(hits, ["a", "b"], 2) == 0.5
    assert stats.mrr_at_k(hits, ["b"], 3) == 1 / 3
    assert 0 < stats.ndcg_at_k(hits, ["a", "b"], 3) < 1
    assert stats.jains_fairness_index([2, 2]) == 1
    assert stats.jains_fairness_index([2, 0]) == 0.5
    exp = Experiment("test", checks=[InvariantCheck("leaks", 1, 0)])
    report = Report(EnvMetadata.capture("quick", 1, [DATA]), [exp]).serialize()
    assert not report["passed"] and not report["experiments"][0]["checks"][0]["passed"]


def test_dataset_and_rag_cli(monkeypatch, tmp_path):
    data = json.loads(DATA.read_text())
    rows = data["queries"]
    assert len(set(r["id"] for r in rows)) == len(rows)
    assert len(set(r["query"] for r in rows)) == len(rows)
    counts = Counter(r["slice"] for r in rows)
    assert len(counts) == 4 and min(counts.values()) >= 30
    sources = {p.name for p in (ROOT / "knowledge/it_helpdesk").glob("*.md")}
    private = {d["source"]: d["department"] for d in data["documents"]}
    for row in rows:
        assert row["relevant_sources"] and set(row["relevant_sources"]) <= sources
        assert row["forbidden_sources"] and all(
            private[s] != row["department"] for s in row["forbidden_sources"]
        )

    class Fake:
        model_name = "offline-test"

        def embed_query(self, text):
            return np.array([1.0, text.count("VPN")], dtype=np.float32)

        def embed_documents(self, docs):
            return [self.embed_query(t) for t in docs]

    monkeypatch.setattr("servicedesk_agent.infra.vector_store.Embedder", Fake)
    subset = dict(data, queries=rows[:4], documents=data["documents"][:6])
    path = tmp_path / "subset.json"
    path.write_text(json.dumps(subset))
    monkeypatch.setattr(rag, "DATA", path)
    out = tmp_path / "ledger.json"
    assert main(["p0", "--only", "rag_hybrid_ablation", "--repeats", "1", "--out", str(out)]) == 0
    report = json.loads(out.read_text())
    assert report["passed"] and len(report["experiments"][0]["arms"]) == 7


@pytest.mark.parametrize(
    "job,params", [(notify, ("quick", 0.05)), (fair, (2, 10, 2)), (fault, (1,))]
)
@pytest.mark.parametrize("protected", [False, True])
def test_workflow_arms(tmp_path, job, params, protected):
    async def run():
        store = WorkflowStore(f"sqlite:///{tmp_path / 'work.db'}")
        try:
            metrics, gates = await job(store, protected, *params)
            if job == fault and not protected:
                assert metrics["duplicate_external_write_rate"] > 0 and gates["missed_drift"] == 1
            else:
                assert not any(gates.values())
        finally:
            await store.aclose()

    asyncio.run(run())


def test_workflow_cli_discards_warmup_and_fails_gate(tmp_path, monkeypatch):
    from servicedesk_agent.cmd import eval as cli

    calls = Counter()

    async def workload(store, protected, *params):
        key = (protected, params)
        calls[key] += 1
        return {"probe": calls[key]}, {"missed": int(protected)}

    for name in ("notify", "fair", "fault"):
        monkeypatch.setattr(cli, name, workload)
    monkeypatch.setattr(cli, "WorkflowStore", lambda url: EmptyStore())
    out = tmp_path / "out.json"
    assert (
        cli.main(["p0", "--only", ",".join(cli.NAMES[1:]), "--repeats", "2", "--out", str(out)])
        == 1
    )
    report = json.loads(out.read_text())
    assert len(report["experiments"]) == 3 and not report["passed"]
    for exp in report["experiments"]:
        assert not exp["passed"]
        for arm in exp["arms"]:
            assert arm["metrics"]["probe"]["samples"] == [2, 3]
    with pytest.raises(SystemExit):
        cli.main(["p0", "--out", str(out)])


class EmptyStore:
    async def aclose(self):
        pass
