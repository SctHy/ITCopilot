"""公开基准 WixQA（Cohen et al. 2025, arXiv:2505.08643, MIT）上的检索评测。

知识库是 Wix 客服中心的 6,221 篇英文文章；查询取两个人工集：ExpertWritten（真实工单 + 专家答案，
200 条）和 Simulated（客服对话提炼，200 条）。不用 LLM 合成的 Synthetic 集。

评测走应用实际的检索链路（KnowledgeBase.search，含切片、BM25、向量与 RRF），按文章计分：
每条查询取前 20 个片段，按出现顺序去重成文章列表，与标注的 article_ids 比对。

    python scripts/evaluate_rag_wixqa.py --fetch                    # 下载固定版本并校验 sha256
    python scripts/evaluate_rag_wixqa.py                            # 默认 embedding（中英 jina-v2-base-zh）
    python scripts/evaluate_rag_wixqa.py --embedding-model BAAI/bge-small-en-v1.5

索引写入 data/external/wixqa/kb-<模型>.sqlite3，再次运行直接复用。
"""

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import statistics
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from evaluation.reporting import percentile, ROOT, write_report

REPO, REVISION = "Wix/WixQA", "d662dc42479c14e202eccd832f8c4b66a035c4cc"
FILES = {
    "corpus": ("wix_kb_corpus/wix_kb_corpus.jsonl", "46b852c60b85fb1828e7c6118bb56ba796eba99120a6b69c6b984c06e1c5e1fe"),
    "expertwritten": ("wixqa_expertwritten/test.jsonl", "a1bb6888a8394f980bc88f0d09b873fcd283ff4c9782600bc203f8103c2f1bad"),
    "simulated": ("wixqa_simulated/test.jsonl", "9a2b9f42c081467b3de5bd92511e770bca3cc7977f169526fd78dd3c985ac4a8"),
}
DATA_DIR = ROOT / "data/external/wixqa"
CHUNK_WINDOW = 20
INGEST_BATCH = 500


def local_path(name: str) -> pathlib.Path:
    return DATA_DIR / FILES[name][0].replace("/", "_")


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name, (remote, expected) in FILES.items():
        path = local_path(name)
        if not path.exists() or sha256(path) != expected:
            urllib.request.urlretrieve(f"https://huggingface.co/datasets/{REPO}/resolve/{REVISION}/{remote}", path)
        if sha256(path) != expected:
            raise SystemExit(f"{path} sha256 不匹配")


def load(name: str) -> list:
    path = local_path(name)
    if not path.exists():
        raise SystemExit(f"缺少 {path}，先运行 --fetch")
    if sha256(path) != FILES[name][1]:
        raise SystemExit(f"{path} sha256 不匹配，重新 --fetch")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]




def scored(case, items, latency_ms):
    articles = list(dict.fromkeys(item["source"] for item in items))
    gold = set(case["article_ids"])
    first = next((i for i, a in enumerate(articles, 1) if a in gold), 0)
    return {
        "id": case["id"], "gold": sorted(gold), "retrieved": articles[:5], "first_rank": first,
        "recall_at_1": len(gold & set(articles[:1])) / len(gold),
        "recall_at_5": len(gold & set(articles[:5])) / len(gold),
        "latency_ms": round(latency_ms, 1),
    }


def summarize(rows):
    n = max(1, len(rows))
    latencies = [r["latency_ms"] for r in rows]
    return {
        "n": len(rows),
        "recall_at_1": round(sum(r["recall_at_1"] for r in rows) / n, 4),
        "recall_at_5": round(sum(r["recall_at_5"] for r in rows) / n, 4),
        "hit_at_5": round(sum(0 < r["first_rank"] <= 5 for r in rows) / n, 4),
        "mrr": round(sum(1 / r["first_rank"] for r in rows if r["first_rank"]) / n, 4),
        "latency_p50_ms": round(statistics.median(latencies), 1) if latencies else 0.0,
        "latency_p95_ms": round(percentile(latencies, 0.95), 1) if latencies else None,
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fetch", action="store_true", help="下载固定版本的数据文件后退出")
    parser.add_argument("--embedding-model", help="覆盖 IT_COPILOT_EMBEDDING_MODEL")
    parser.add_argument("--modes", default="bm25,vector,hybrid")
    parser.add_argument("--limit", type=int, default=0, help="每个查询集只跑前 N 条；0 = 全部")
    args = parser.parse_args()
    if args.fetch:
        fetch()
        return
    if args.embedding_model:
        os.environ["IT_COPILOT_EMBEDDING_MODEL"] = args.embedding_model
    os.environ["IT_COPILOT_SEED_DEMO_DOCS"] = "false"
    from core.embedding import embedding_model_name
    from tooling.knowledge_base import KnowledgeBase

    model = embedding_model_name()
    corpus = load("corpus")
    suites = {name: load(name) for name in ("expertwritten", "simulated")}
    for name, cases in suites.items():
        for i, case in enumerate(cases):
            case["id"] = f"{name}-{i:03d}"
    if args.limit:
        suites = {name: cases[: args.limit] for name, cases in suites.items()}

    db_path = DATA_DIR / f"kb-{model.replace('/', '_')}.sqlite3"
    kb = KnowledgeBase(f"sqlite:///{db_path}")
    ingest_s = None
    if not await kb.doc_count_async():
        # source 存文章 id，检索结果据此还原到文章级；标题在语料中不唯一。
        docs = [{"document_id": d["id"], "title": d["title"] or d["url"], "content": d["contents"], "source": d["id"]}
                for d in corpus if d["contents"].strip()]
        started = time.monotonic()
        for i in range(0, len(docs), INGEST_BATCH):
            await kb.add_documents_async(docs[i:i + INGEST_BATCH])
            print(f"ingested {min(i + INGEST_BATCH, len(docs))}/{len(docs)}", file=sys.stderr, flush=True)
        ingest_s = round(time.monotonic() - started, 1)
    chunks = await kb.doc_count_async()

    strategies = {}
    for mode in args.modes.split(","):
        per_suite = {}
        for name, cases in suites.items():
            rows = []
            for case in cases:
                started = time.monotonic()
                items = await kb.search_async(case["question"], top_k=CHUNK_WINDOW, mode=mode)
                rows.append(scored(case, items, (time.monotonic() - started) * 1000))
            per_suite[name] = {**summarize(rows), "cases": rows}
            print(mode, name, json.dumps(summarize(rows)), file=sys.stderr, flush=True)
        strategies[mode] = per_suite

    write_report(
        f"rag_wixqa_{model.split('/')[-1]}.json",
        {
            "suite": "wixqa",
            "status": "complete" if not args.limit else "partial",
            "source": {"dataset": REPO, "revision": REVISION, "license": "MIT", "paper": "arXiv:2505.08643",
                       "sha256": {name: sha for name, (_, sha) in FILES.items()}},
            "embedding_model": model,
            "articles": len(corpus),
            "chunks": chunks,
            "ingest_seconds": ingest_s,
            "chunk_window": CHUNK_WINDOW,
            "scoring": "article-level; top-20 chunks deduplicated by article in rank order",
            "strategies": strategies,
        },
        echo=False,
    )
    print(json.dumps({mode: {s: {k: v for k, v in r.items() if k != "cases"} for s, r in per.items()}
                      for mode, per in strategies.items()}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
