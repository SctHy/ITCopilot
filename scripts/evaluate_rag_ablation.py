"""60 authored documents / 40 fixed queries, ACL distractors and rank diagnostics.

Optional live rewrite+rerank is measured on the first --live-limit queries (default: all),
with a paired plain-hybrid baseline. Citation presence is not labelled faithfulness.
"""

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from evaluation.reporting import ROOT, DATASETS_DIR, write_report
from tooling.knowledge_base import KnowledgeBase
from tooling.tool_manager import MCPToolManager, Tool
from tooling.knowledge_base import KNOWLEDGE_SEARCH_SCHEMA


def summarize(rows):
    n = max(1, len(rows))
    return {
        "n": len(rows),
        "recall_at_3": sum(0 < r["rank"] <= 3 for r in rows) / n,
        "mrr": sum(1 / r["rank"] if r["rank"] else 0 for r in rows) / n,
        "department_leaks": sum(r["department_leak"] for r in rows),
        "mean_latency_ms": sum(r["latency_ms"] for r in rows) / n,
        "cases": rows,
    }


def scored(case, items, started):
    rank = next(
        (i for i, item in enumerate(items, 1) if item["title"] in case["relevant_titles"]), 0
    )
    return {
        "id": case["id"],
        "rank": rank,
        "titles": [x["title"] for x in items],
        "department_leak": any(x["department"] not in {"*", case["department"]} for x in items),
        "latency_ms": (time.monotonic() - started) * 1000,
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--database-url", help="empty database to evaluate on (default: temporary SQLite)")
    parser.add_argument("--live-limit", type=int, default=0, help="live rewrite+rerank on the first N queries; 0 = all")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    os.environ["IT_COPILOT_SEED_DEMO_DOCS"] = "false"
    source = DATASETS_DIR / "rag_v2_corpus.json"
    docs = json.loads(source.read_text())
    queries = json.loads((DATASETS_DIR / "rag_v2_queries.json").read_text())
    reports = {}
    with tempfile.TemporaryDirectory(prefix="rag-ablation-") as path:
        kb = KnowledgeBase(args.database_url or f"sqlite:///{path}/kb.sqlite3")
        await kb.add_documents_async(docs)
        for mode in ("bm25", "vector", "hybrid"):
            rows = []
            for case in queries:
                started = time.monotonic()
                items = await kb.search_async(
                    case["query"], top_k=3, department=case["department"], mode=mode
                )
                rows.append(scored(case, items, started))
            reports[mode] = summarize(rows)
        if args.live:
            manager = MCPToolManager(
                os.getenv("OPENAI_API_KEY", "sk-local"),
                os.getenv("OPENAI_BASE_URL"),
                os.getenv("OPENAI_MODEL", ""),
            )
            manager.register(
                Tool("knowledge_search", "IT knowledge", kb.search_handler, KNOWLEDGE_SEARCH_SCHEMA)
            )
            rows = []
            live_cases = queries[: args.live_limit or len(queries)]
            for case in live_cases:
                started = time.monotonic()
                result = await manager.search_with_rewrite(
                    "knowledge_search", case["query"], top_k=3, department=case["department"]
                )
                row = scored(case, result.data or [], started)
                row.update(success=result.success, reranked=result.reranked, error=result.error)
                rows.append(row)
            reports["hybrid_rewrite_rerank"] = summarize(rows)
            reports["hybrid_paired"] = summarize(reports["hybrid"]["cases"][: len(live_cases)])
    write_report(
        "rag_ablation_v2.json",
        {
            "suite": "rag-v2",
            "status": "complete",
            "documents": len(docs),
            "queries": len(queries),
            "live_rerank": args.live,
            "corpus_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "strategies": reports,
            "limitations": "Authored sandbox corpus, 20 topics with financial and historical distractors. Does not measure answer faithfulness or production recall.",
        },
        echo=False,
    )
    print(
        json.dumps(
            {k: {a: b for a, b in v.items() if a != "cases"} for k, v in reports.items()},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
