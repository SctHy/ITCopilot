"""Source-level IR at the first k returned chunks, plus isolated BM25 kernel timings."""

from collections import defaultdict
from functools import lru_cache
import json
import math
from time import perf_counter
from unittest.mock import patch

import numpy as np

from it_copilot.agent.tooling.knowledge_base import KnowledgeBase, KnowledgeLibrary
from .ledger import DATA, ROOT, Experiment, InvariantCheck
from .stats import arm, measured_rounds, compare, mrr_at_k, ndcg_at_k, percentile, recall_at_k


class Corpus:
    """In-memory chunk reader for the lexical-only scaling experiment."""

    def __init__(self, chunks, size):
        self.documents = [chunks[i % len(chunks)] for i in range(size)]

    def collection(self, name):
        return self

    def version(self):
        return len(self.documents), 0

    def get(self):
        return dict(
            ids=list(map(str, range(len(self.documents)))),
            documents=self.documents,
            metadatas=[{}] * len(self.documents),
        )


def scaling(exp, chunks, size, queries, repeats):
    corpus = Corpus(chunks, size)
    lib = KnowledgeLibrary(corpus, "scale")
    lengths = lib._lengths.tolist()
    avg = max(1, sum(lengths) / size)
    postings = {t: list(zip(pos.tolist(), tf.tolist())) for t, (pos, tf) in lib._postings.items()}
    mask = np.ones(size, dtype=bool)

    def scalar(query):
        scores = [0.0] * size
        for term in set(lib._tokens(query)):
            hits = postings.get(term, [])
            idf = math.log(1 + (size - len(hits) + 0.5) / (len(hits) + 0.5))
            for pos, tf in hits:
                scores[pos] += idf * tf * 2 / (tf + 1.5 * (0.25 + 0.75 * lengths[pos] / avg))
        return scores

    arms = []
    for name, fn in [
        ("python_loop_bm25_baseline", scalar),
        ("numpy_vectorized_prod", lambda q: lib._bm25_array(q, mask)),
    ]:
        samples = defaultdict(list)
        for measured in measured_rounds(repeats):
            times = []
            for q in queries:
                start = perf_counter()
                scores = fn(q)
                times.append((perf_counter() - start) * 1000)
                if not measured and not np.allclose(scores, scalar(q)):
                    raise ValueError("BM25 score mismatch")
            if measured:
                for p in (50, 95):
                    samples[f"p{p}_ms"].append(percentile(times, p))
        arms.append(arm(name, samples))
    exp.arms.extend(arms)
    for key in arms[0].metrics:
        compare(exp, *arms, key, "Dense score buffers and precomputed postings consume memory.")


def rag(url, profile, repeats):
    data = json.loads(DATA.read_text())
    docs = [
        dict(source=p.name, title=p.stem, content=p.read_text())
        for p in sorted((ROOT / "knowledge/it_helpdesk").glob("*.md"))
    ]
    size = 3000 if profile == "quick" else 5000
    exp = Experiment(
        "rag_hybrid_ablation",
        dict(chunks=size, quality_unit="mean per query across measured rounds"),
    )
    with patch.dict("os.environ", IT_COPILOT_SEED_DEMO_DOCS="false"):
        kb = KnowledgeBase(url)
    try:
        embedder = kb._store.embedder
        exp.settings["embedding"] = embedder.model_name
        # FastEmbed looks in its configured local cache before fetching a missing model.
        try:
            embedder.embed_query("本地模型可用性检查")
        except (OSError, ValueError, RuntimeError) as exc:
            exp.settings["model_load_error"] = f"{type(exc).__name__}: {exc}"
            exp.checks.append(InvariantCheck("local_embedding_available", 0, 1))
            return exp
        kb.add_documents(docs + data["documents"], tenant="evaluation")
        kb.add_documents(
            [dict(title="租户探针", source="foreign.md", content="VPN 客户端版本去哪检查")],
            tenant="foreign",
        )
        # Ranking quality only. Reuse the original model's exact query vector across arms.
        embedder.embed_query = lru_cache(maxsize=512)(embedder.embed_query)
        forbidden = foreign = 0
        arms = {}
        for name, mode in [
            ("bm25_only", "bm25"),
            ("vector_only", "vector"),
            ("hybrid_rrf_prod", "hybrid"),
            ("post_filter_baseline", "hybrid"),
            ("sql_prefilter_prod", "hybrid"),
        ]:
            samples = defaultdict(lambda: defaultdict(list))
            for measured in measured_rounds(repeats):
                for row in data["queries"]:
                    sliced = row["slice"]
                    isolation = name in ("post_filter_baseline", "sql_prefilter_prod")
                    if isolation and sliced != "dept_isolation":
                        continue
                    dept = row["department"]
                    hits = kb.search(
                        row["query"],
                        tenant="evaluation",
                        department=None if name == "post_filter_baseline" else dept,
                        mode=mode,
                    )
                    if name == "post_filter_baseline":
                        hits = [h for h in hits if h["department"] in ("*", dept)]
                    sources = [h["source"] for h in hits]
                    leaks = sum(
                        h["department"] not in ("*", dept)
                        or h["source"] in row["forbidden_sources"]
                        for h in hits
                    )
                    forbidden += leaks
                    foreign += sources.count("foreign.md")
                    relevant = row["relevant_sources"]
                    for group in [sliced] if isolation else ["all", sliced]:
                        metrics = {
                            "recall@3": recall_at_k(sources, relevant, 3),
                            "recall@5": recall_at_k(sources, relevant, 5),
                            "mrr@5": mrr_at_k(sources, relevant, 5),
                            "ndcg@5": ndcg_at_k(sources, relevant, 5),
                        }
                        if isolation:
                            metrics["leaking_query_rate"] = bool(leaks)
                        if measured:
                            for key, val in metrics.items():
                                samples[f"{group}/{key}"][row["id"]].append(val)
            arms[name] = arm(
                name, {k: [np.mean(v) for v in by_id.values()] for k, by_id in samples.items()}
            )
        exp.arms.extend(arms.values())
        for name in ("bm25_only", "vector_only"):
            for key in arms[name].metrics:
                compare(
                    exp,
                    arms[name],
                    arms["hybrid_rrf_prod"],
                    key,
                    "Hybrid executes lexical and vector retrieval.",
                )
        compare(
            exp,
            arms["post_filter_baseline"],
            arms["sql_prefilter_prod"],
            "dept_isolation/recall@5",
            "SQL limits candidates before ranking.",
        )
        same_query = kb.search("VPN 客户端版本去哪检查", tenant="foreign")
        foreign += sum(hit["source"] != "foreign.md" for hit in same_query)
        exp.checks = [
            InvariantCheck(
                "foreign_probe_retrievable",
                int(any(hit["source"] == "foreign.md" for hit in same_query)),
                1,
            ),
            InvariantCheck("cross_department_hits", forbidden, 0),
            InvariantCheck("cross_tenant_hits", foreign, 0),
        ]
        scaling(
            exp,
            [chunk for d in docs for chunk in kb.library("evaluation")._chunk_text(d["content"])],
            size,
            [q["query"] for q in data["queries"] if q["slice"] == "exact_code"],
            repeats,
        )
        return exp
    finally:
        kb._store.dispose()
