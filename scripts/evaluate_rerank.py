#!/usr/bin/env python3
"""WixQA 上的结果重排对比：不重排 / LLM 重排 / Jev 重排。

候选是应用 hybrid 检索的前 20 个片段；重排走 MCPToolManager._rerank_with_status 的线上实现，
取重排后的前 10 个，其余按原顺序接在后面，再按文章去重计分（与 evaluate_rag_wixqa.py 同口径）。
需要先跑过 evaluate_rag_wixqa.py 建好索引；会调用付费 API（LLM 与 Jev 各 400 次左右）。

    python scripts/evaluate_rerank.py --limit 5          # 小规模确认
    python scripts/evaluate_rerank.py --chars 200,600    # 不同候选文本长度
"""
import argparse
import asyncio
import importlib.util
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
os.environ["IT_COPILOT_SEED_DEMO_DOCS"] = "false"

from core.jev import JevClient
from evaluation.reporting import write_report
from tooling.tool_manager import MCPToolManager

_spec = importlib.util.spec_from_file_location("wixqa", ROOT / "scripts/evaluate_rag_wixqa.py")
wixqa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wixqa)

CANDIDATES, RERANK_TOP = 20, 10


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--strategies", default="hybrid,llm,jev")
    parser.add_argument("--chars", default="200", help="候选文本截断长度，逗号分隔；线上默认 200")
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    from core.embedding import embedding_model_name
    from tooling.knowledge_base import KnowledgeBase

    model = embedding_model_name()
    kb = KnowledgeBase(f"sqlite:///{wixqa.DATA_DIR / ('kb-' + model.replace('/', '_') + '.sqlite3')}")
    if not await kb.doc_count_async():
        raise SystemExit("索引为空：先运行 scripts/evaluate_rag_wixqa.py")
    suites = {}
    for name in ("expertwritten", "simulated"):
        cases = wixqa.load(name)
        for i, case in enumerate(cases):
            case["id"] = f"{name}-{i:03d}"
        suites[name] = cases[: args.limit] if args.limit else cases

    candidates = {}
    for cases in suites.values():
        for case in cases:
            items = await kb.search_async(case["question"], top_k=CANDIDATES, mode="hybrid")
            # 只保留重排需要看到的字段，避免分数、id 等元数据占掉截断长度。
            candidates[case["id"]] = [{"title": x["title"], "content": x["content"], "source": x["source"]} for x in items]

    llm_manager = MCPToolManager(os.getenv("OPENAI_API_KEY", "sk-local"), os.getenv("OPENAI_BASE_URL"), os.getenv("OPENAI_MODEL", ""))
    jev = JevClient.from_env(timeout_s=30, retries=2)
    jev_manager = MCPToolManager("sk-local", jev=jev) if jev else None
    semaphore = asyncio.Semaphore(args.concurrency)

    async def run(strategy, case):
        items = candidates[case["id"]]
        async with semaphore:
            started = time.monotonic()
            reranked = False
            if strategy == "hybrid":
                ordered = items
            else:
                manager = llm_manager if strategy == "llm" else jev_manager
                head, reranked = await manager._rerank_with_status(case["question"], [dict(x) for x in items], RERANK_TOP)
                picked = {json.dumps(x, sort_keys=True) for x in head}
                ordered = head + [x for x in items if json.dumps(x, sort_keys=True) not in picked]
            row = wixqa.scored(case, ordered, (time.monotonic() - started) * 1000)
        row["reranked"] = reranked
        return row

    strategies = {}
    try:
        for chars in args.chars.split(","):
            os.environ["IT_COPILOT_RERANK_CHARS"] = chars
            for strategy in args.strategies.split(","):
                if strategy == "jev" and not jev_manager:
                    print("缺少 TYPESAFE_API_KEY，跳过 jev", file=sys.stderr)
                    continue
                key = strategy if strategy == "hybrid" else f"{strategy}@{chars}"
                if key in strategies:
                    continue
                per_suite = {}
                for name, cases in suites.items():
                    rows = await asyncio.gather(*(run(strategy, case) for case in cases))
                    failed = sum(not r["reranked"] for r in rows) if strategy != "hybrid" else 0
                    per_suite[name] = {**wixqa.summarize(rows), "rerank_failed": failed, "cases": rows}
                    print(key, name, json.dumps({k: v for k, v in per_suite[name].items() if k != "cases"}), file=sys.stderr, flush=True)
                strategies[key] = per_suite
    finally:
        if jev:
            await jev.aclose()

    write_report("rerank_wixqa.json", {
        "suite": "wixqa-rerank", "status": "partial" if args.limit else "complete", "embedding_model": model,
        "candidates": CANDIDATES, "rerank_top": RERANK_TOP, "llm_model": os.getenv("OPENAI_MODEL", ""),
        "scoring": wixqa.__dict__.get("CHUNK_WINDOW") and "article-level, same as rag_wixqa", "strategies": strategies,
    }, echo=False)
    print(f"{'strategy':<12}{'suite':<15}{'R@1':>7}{'R@5':>7}{'MRR':>7}{'p50ms':>9}{'p95ms':>9}{'failed':>8}")
    for key, per in strategies.items():
        for name, r in per.items():
            print(f"{key:<12}{name:<15}{r['recall_at_1']:>7.3f}{r['recall_at_5']:>7.3f}{r['mrr']:>7.3f}{r['latency_p50_ms']:>9.0f}{r['latency_p95_ms']:>9.0f}{r['rerank_failed']:>8}")


if __name__ == "__main__":
    asyncio.run(main())
