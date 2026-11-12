"""真实企业 IT 文档上的检索评测：GitLab 公开 Handbook（CC BY-SA 4.0）。

语料是 GitLab 公司内部真实在用、对外公开的 IT / 安全 / 业务系统文档（corporate IT 服务台、Okta、
权限申请、设备、安全策略等），查询取文档里 GitLab 员工自己写的 FAQ 问题标题（以 ? 结尾的 ##–#### 标题），
标注是“问题所在的文档”——数据和标注都不经本项目作者之手。

防泄漏：建索引前把所有问题标题行从正文删掉，查询只能靠答案正文召回，不能靠标题原文字面命中。

    # 1) 从下载的 handbook 归档解压目录生成语料与查询（只读文本，不执行任何内容）
    python -I scripts/evaluate_rag_gitlab.py --prepare /tmp/itc_ext/gitlab_raw
    # 2) 跑检索（走应用实际的 KnowledgeBase.search 链路）
    python scripts/evaluate_rag_gitlab.py
    # 3) 干扰实验：把 Handbook 混入项目自写语料，看自写查询的召回是否下降
    python scripts/evaluate_rag_gitlab.py --distractor

评分按文档：前 20 个片段按顺序去重成文档列表，与问题所在文档比对；另报“答案片段命中”：
前 5 个片段里是否有一个与该问题的答案正文重叠（取答案前 80 个字符做子串匹配）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import re
import statistics
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data/external/gitlab_handbook"
COMMIT = "7a669fd029d42a26b473b51d7fdf19af00232817"
CHUNK_WINDOW = 20
INGEST_BATCH = 200
# 查询所在的 IT 相关目录；其余目录（客服系统、产品安全等）只作为真实噪声参与检索。
IT_SECTIONS = ("eta/corporate-it", "security/corporate", "security/identity", "business-technology",
               "security/policies_and_standards", "security/security-and-technology-policies")
QUESTION = re.compile(r"^(#{2,4})\s+(.+\?)\s*$")
HEADING = re.compile(r"^(#{1,6})\s+")


def clean(text: str) -> str:
    text = re.sub(r"\{\{[<%].*?[%>]\}\}", " ", text, flags=re.S)        # Hugo shortcodes
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)                    # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)                 # links -> anchor text
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()


def front_matter(raw: str):
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end > 0:
            meta, body = raw[3:end], raw[end + 4:]
            title = re.search(r"^title:\s*['\"]?(.+?)['\"]?\s*$", meta, re.M)
            return (title.group(1) if title else ""), body
    return "", raw


def prepare(raw_dir: pathlib.Path) -> None:
    docs, queries = [], []
    for path in sorted(raw_dir.rglob("*.md")):
        rel = path.as_posix().split("/content/handbook/", 1)[-1]
        title, body = front_matter(path.read_text(encoding="utf-8", errors="replace"))
        lines = body.splitlines()
        kept, i = [], 0
        while i < len(lines):
            m = QUESTION.match(lines[i])
            if not m:
                kept.append(lines[i])
                i += 1
                continue
            level, question = len(m.group(1)), m.group(2).strip()
            j = i + 1
            while j < len(lines) and not ((h := HEADING.match(lines[j])) and len(h.group(1)) <= level):
                j += 1
            answer = clean("\n".join(lines[i + 1:j]))
            words = re.sub(r"[*_`]", "", question).split()
            if len(words) >= 4 and len(answer) >= 150 and rel.startswith(IT_SECTIONS):
                queries.append({"question": re.sub(r"[*_`]", "", question), "doc": rel, "answer_head": answer[:80]})
            i += 1                                                         # 只删问题标题行，答案正文保留
        content = clean("\n".join(kept))
        if len(content) >= 200:
            docs.append({"id": rel, "title": title or rel, "content": content})
    doc_ids = {d["id"] for d in docs}
    seen, unique = set(), []
    for q in queries:                                                      # 同一问题出现在多篇文档时无唯一标注，丢弃
        key = q["question"].lower()
        if q["doc"] in doc_ids and key not in seen and sum(x["question"].lower() == key for x in queries) == 1:
            seen.add(key)
            unique.append({**q, "id": f"q{len(unique):03d}"})
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "corpus.jsonl").write_text("".join(json.dumps(d, ensure_ascii=False) + "\n" for d in docs), encoding="utf-8")
    (DATA_DIR / "queries.jsonl").write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in unique), encoding="utf-8")
    manifest = {"source": "https://gitlab.com/gitlab-com/content-sites/handbook", "commit": COMMIT, "license": "CC BY-SA 4.0",
                "documents": len(docs), "queries": len(unique), "query_sections": IT_SECTIONS,
                "corpus_sha256": hashlib.sha256((DATA_DIR / "corpus.jsonl").read_bytes()).hexdigest(),
                "queries_sha256": hashlib.sha256((DATA_DIR / "queries.jsonl").read_bytes()).hexdigest()}
    (DATA_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0.0


def summarize(rows):
    n = max(1, len(rows))
    lat = [r["latency_ms"] for r in rows]
    out = {"n": len(rows),
           "hit_at_1": round(sum(r["first_rank"] == 1 for r in rows) / n, 4),
           "hit_at_5": round(sum(0 < r["first_rank"] <= 5 for r in rows) / n, 4),
           "mrr": round(sum(1 / r["first_rank"] for r in rows if r["first_rank"]) / n, 4),
           "latency_p50_ms": round(statistics.median(lat), 1) if lat else 0.0,
           "latency_p95_ms": round(percentile(lat, 0.95), 1)}
    if rows and "answer_hit_at_5" in rows[0]:
        out["answer_chunk_hit_at_5"] = round(sum(r["answer_hit_at_5"] for r in rows) / n, 4)
    return out


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


async def build(kb, docs):
    if await kb.doc_count_async():
        return None
    started = time.monotonic()
    for i in range(0, len(docs), INGEST_BATCH):
        await kb.add_documents_async(docs[i:i + INGEST_BATCH])
        print(f"ingested {min(i + INGEST_BATCH, len(docs))}/{len(docs)}", file=sys.stderr, flush=True)
    return round(time.monotonic() - started, 1)


async def run_handbook(kb_cls, model, modes):
    from evaluation.reporting import write_report
    docs = [json.loads(x) for x in (DATA_DIR / "corpus.jsonl").read_text(encoding="utf-8").splitlines()]
    queries = [json.loads(x) for x in (DATA_DIR / "queries.jsonl").read_text(encoding="utf-8").splitlines()]
    kb = kb_cls(f"sqlite:///{DATA_DIR / ('kb-' + model.replace('/', '_') + '.sqlite3')}")
    ingest_s = await build(kb, [{"document_id": d["id"], "title": d["title"], "content": d["content"], "source": d["id"]} for d in docs])
    strategies = {}
    for mode in modes:
        rows = []
        for q in queries:
            started = time.monotonic()
            items = await kb.search_async(q["question"], top_k=CHUNK_WINDOW, mode=mode)
            latency = (time.monotonic() - started) * 1000
            ranked = list(dict.fromkeys(item["source"] for item in items))
            first = next((i for i, a in enumerate(ranked, 1) if a == q["doc"]), 0)
            head = norm(q["answer_head"])[:60]
            rows.append({"id": q["id"], "question": q["question"], "gold": q["doc"], "retrieved": ranked[:5], "first_rank": first,
                         "answer_hit_at_5": any(head and head in norm(item["content"]) for item in items[:5]),
                         "section": q["doc"].split("/")[0] + "/" + q["doc"].split("/")[1], "latency_ms": round(latency, 1)})
        by_section = {}
        for r in rows:
            by_section.setdefault(r["section"], []).append(r)
        strategies[mode] = {**summarize(rows), "by_section": {k: summarize(v) for k, v in sorted(by_section.items())}, "cases": rows}
        print(mode, json.dumps(summarize(rows)), file=sys.stderr, flush=True)
    manifest = json.loads((DATA_DIR / "manifest.json").read_text())
    write_report("deep_20261112/rag_gitlab_handbook.json", {
        "suite": "gitlab_handbook_faq", "source": manifest, "embedding_model": model,
        "documents": len(docs), "chunks": await kb.doc_count_async(), "ingest_seconds": ingest_s,
        "scoring": "doc-level over top-20 chunks deduplicated in rank order; answer_chunk_hit_at_5 = a top-5 chunk contains the first 60 chars of the gold answer",
        "leakage_control": "all question-heading lines removed from indexed text",
        "strategies": strategies}, echo=False)
    return {m: {k: v for k, v in s.items() if k not in ("cases", "by_section")} for m, s in strategies.items()}


async def run_distractor(kb_cls, model, modes, queries_path):
    """自写语料（61 篇）单独建库 vs. 混入 Handbook 后建库，自写查询的召回变化。"""
    from evaluation.reporting import write_report
    own = json.loads((ROOT / "evaluation/datasets/rag_v2_corpus.json").read_text(encoding="utf-8"))
    queries = json.loads(pathlib.Path(queries_path).read_text(encoding="utf-8"))
    handbook = [json.loads(x) for x in (DATA_DIR / "corpus.jsonl").read_text(encoding="utf-8").splitlines()]
    own_docs = [{"title": d["title"], "content": d["content"], "department": d.get("department", "all"), "source": "own:" + d["title"]} for d in own]
    hb_docs = [{"document_id": d["id"], "title": d["title"], "content": d["content"], "source": "hb:" + d["id"]} for d in handbook]
    results = {}
    for name, docs in (("own_only", own_docs), ("own_plus_handbook", own_docs + hb_docs)):
        kb = kb_cls(f"sqlite:///{DATA_DIR / ('distractor-' + name + '-' + model.replace('/', '_') + '.sqlite3')}")
        await build(kb, docs)
        per_mode = {}
        for mode in modes:
            rows = []
            for q in queries:
                started = time.monotonic()
                items = await kb.search_async(q["query"], top_k=CHUNK_WINDOW, mode=mode, department=q.get("department"))
                latency = (time.monotonic() - started) * 1000
                ranked = list(dict.fromkeys(item["title"] for item in items))
                gold = set(q.get("relevant_titles") or [])
                first = next((i for i, a in enumerate(ranked, 1) if a in gold), 0) if gold else 0
                rows.append({"id": q["id"], "first_rank": first, "has_gold": bool(gold), "latency_ms": round(latency, 1),
                             "handbook_in_top3": sum(str(item.get("source", "")).startswith("hb:") for item in items[:3])})
            rows = [r for r in rows if r["has_gold"]]
            per_mode[mode] = {**summarize(rows), "handbook_chunks_in_top3_mean": round(statistics.mean(r["handbook_in_top3"] for r in rows), 3), "cases": rows}
            print(name, mode, json.dumps(summarize(rows)), file=sys.stderr, flush=True)
        results[name] = per_mode
    write_report("deep_20261112/rag_distractor.json", {"suite": "own_queries_with_real_distractors", "embedding_model": model,
                 "own_documents": len(own_docs), "handbook_documents": len(hb_docs), "results": results}, echo=False)
    return {n: {m: {k: v for k, v in s.items() if k != "cases"} for m, s in per.items()} for n, per in results.items()}


async def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prepare", help="handbook 归档解压目录")
    p.add_argument("--distractor", action="store_true")
    p.add_argument("--queries", default=str(ROOT / "data/external/gitlab_handbook/rag_v2_queries.json"),
                   help="自写查询（rag_v2_queries.json，已从工作区删除时从 git HEAD 导出）")
    p.add_argument("--modes", default="bm25,vector,hybrid")
    args = p.parse_args()
    if args.prepare:
        prepare(pathlib.Path(args.prepare))
        return
    sys.path.insert(0, str(ROOT))
    os.environ["IT_COPILOT_SEED_DEMO_DOCS"] = "false"
    from core.embedding import embedding_model_name
    from tooling.knowledge_base import KnowledgeBase
    model, modes = embedding_model_name(), args.modes.split(",")
    out = await (run_distractor(KnowledgeBase, model, modes, args.queries) if args.distractor else run_handbook(KnowledgeBase, model, modes))
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
