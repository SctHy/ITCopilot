"""Frozen Gretel test split through the real masking HTTP boundary, local inference only.

Install evaluation-only dependency: uv pip install --python .venv/bin/python pyarrow==26.0.1
The provider is a MockTransport; no document is uploaded. No detector is tuned here.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx2

from core.local_pii import REVISION, detector
from core.model_masking import MaskingTransport, masking_scope
from evaluation.masking_benchmark import EXCLUDED, LABEL_MAP, emitted_spans, rates, score

DATA_REVISION = "7b844d16738527a04264f50214cb426a4cea0897"
DATA_SHA = "014e1057978f030fce4f4cad7c93b9e3377fd0ded713d2bf4c7de00e3e3c8c72"
PREFIX = "Document:\n"  # A fixed wrapper prevents JSON pretty-printing from changing offsets.
SUM_KEYS = ("gold", "predicted", "tp", "fp", "fn", "untyped_tp", "fully_covered",
            "gold_chars", "covered_gold_chars", "masked_non_gold_chars",
            "leaking_document", "negative_document", "negative_changed")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_rows(path):
    import pyarrow.parquet as pq
    if sha(path) != DATA_SHA:
        raise ValueError("Dataset checksum differs from the frozen test split")
    raw = pq.read_table(path).to_pylist()
    rows, seen, duplicates, invalid = [], set(), [], []
    labels, languages = Counter(), Counter()
    for index, row in enumerate(raw):
        text = row["generated_text"]
        spans = json.loads(row["pii_spans"])
        labels.update(s["label"] for s in spans)
        languages[row["language"]] += 1
        if any(s["label"] not in LABEL_MAP and s["label"] not in EXCLUDED for s in spans):
            raise ValueError("Unmapped dataset label")
        if any(not 0 <= s["start"] < s["end"] <= len(text) for s in spans):
            invalid.append(index)
            continue  # Entire document quarantined, not selective removal of bad entities.
        digest = hashlib.sha256(text.encode()).hexdigest()
        if digest in seen:
            duplicates.append(index)
            continue
        seen.add(digest)
        # Exact duplicate annotations are not independent entities.
        spans = list({(s["start"], s["end"], s["label"]): s for s in spans}.values())
        rows.append({"id": index, "hash": digest, "language": row["language"],
                     "format": row["document_type"], "text": text, "annotations": spans})
    return rows, {"raw_documents": len(raw), "eligible_documents": len(rows),
                  "quarantined_document_ids": invalid, "duplicate_document_ids": duplicates,
                  "raw_labels": dict(labels), "raw_languages": dict(languages)}


def latency(values):
    ordered = sorted(values)
    return {"p50_ms": statistics.median(ordered),
            "p95_ms": ordered[int(.95 * (len(ordered) - 1))],
            "p99_ms": ordered[int(.99 * (len(ordered) - 1))]}


def aggregate(records):
    counts = {key: sum(r[key] for r in records) for key in SUM_KEYS}
    result = rates(counts)
    positive = sum(r["gold"] > 0 for r in records)
    result.update(documents=len(records), sensitive_documents=positive,
                  document_leak_rate=counts["leaking_document"] / positive if positive else None,
                  negative_change_rate=counts["negative_changed"] / counts["negative_document"] if counts["negative_document"] else None,
                  blocked=sum(r["blocked"] for r in records),
                  restoration_failed=sum(not r["restoration_ok"] for r in records),
                  latency=latency([r["latency_ms"] for r in records]))
    # Document bootstrap, conditional on this synthetic corpus, not a production CI.
    import numpy as np
    rng = np.random.default_rng(20261114)
    a = np.array([[r["tp"], r["gold"], r["predicted"], r["fully_covered"]] for r in records])
    sums = np.array([a[rng.integers(0, len(a), len(a))].sum(axis=0) for _ in range(1000)])
    for name, numerator, denominator in [
        ("f1", 2 * sums[:, 0], sums[:, 1] + sums[:, 2]),
        ("full_coverage", sums[:, 3], sums[:, 1]),
    ]:
        vals = np.divide(numerator, denominator, out=np.zeros(1000), where=denominator != 0)
        result[name + "_bootstrap_95ci"] = np.quantile(vals, [.025, .975]).tolist()
    return result


async def run(args):
    rows, quality = load_rows(args.dataset)
    if args.limit:
        rows = rows[:args.limit]
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    source_hashes = {p: sha(ROOT / p) for p in [
        "core/local_pii.py", "core/model_masking.py", "core/redaction.py", "core/pii_rules.py",
        "evaluation/masking_benchmark.py", "scripts/evaluate_masking_public.py"]}
    manifest = {
        "dataset": "gretelai/synthetic_pii_finance_multilingual", "revision": DATA_REVISION,
        "file": "data/test-00000-of-00001.parquet", "sha256": DATA_SHA,
        "license": "Apache-2.0", "quality": quality, "label_map": LABEL_MAP,
        "excluded_labels": EXCLUDED, "prefix": PREFIX, "source_sha256": source_hashes,
        "model_revision": REVISION, "threshold": .8, "timeout_s": 10,
        "limit": args.limit, "documents_to_run": len(rows),
        "python": platform.python_version(), "platform": platform.platform(),
        "notes": ["External synthetic test corpus; independence from pretraining not established",
                  "All seven languages reported; AstrLink upstream evaluation only covers English/Chinese",
                  "Raw source annotations retained; no correction based on predictions",
                  "Zero-in-scope-label documents are not guaranteed free of unannotated PII",
                  "Cold per-document cache; serial paired requests; mock upstream echoes tool arguments",
                  "Strict typed spans vs source labels; all characters required for complete coverage"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    model = detector()
    os.environ["IT_COPILOT_MASK_MODEL_THRESHOLD"] = "0.8"
    os.environ["IT_COPILOT_MASK_MODEL_TIMEOUT_S"] = "10"
    os.environ["IT_COPILOT_MASK_WORDS"] = "[]"
    captured = []

    def upstream(request):
        body = json.loads(request.content)
        masked = body["messages"][0]["content"]
        captured.append(masked)
        return httpx2.Response(200, json={"choices": [{"message": {"role": "assistant", "tool_calls": [
            {"id": "call_eval", "type": "function", "function": {"name": "inspect_document",
             "arguments": json.dumps({"document": masked}, ensure_ascii=False)}}]}}]})

    all_records = defaultdict(list)
    start = time.monotonic()
    with (output / "documents.jsonl").open("w") as stream:
        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
            for i, row in enumerate(rows):
                text = PREFIX + row["text"]
                annotations = [s for s in row["annotations"] if s["label"] in LABEL_MAP]
                gold = [(s["start"] + len(PREFIX), s["end"] + len(PREFIX), LABEL_MAP[s["label"]]) for s in annotations]
                for mode in ("off", "on"):
                    os.environ["IT_COPILOT_MASK_MODEL"] = mode
                    model.cache.clear()
                    captured.clear()
                    predicted, blocked, restored = [], False, False
                    began = time.perf_counter()
                    with masking_scope("benchmark", str(row["id"]) + mode) as session:
                        try:
                            response = await client.post("http://fixture/v1/chat/completions", json={
                                "model": "fixture", "messages": [{"role": "user", "content": text}]})
                        except Exception as exc:
                            if captured:
                                raise  # A sent request with a failed response must not count as blocked.
                            blocked = True
                            error = type(exc).__name__
                        else:
                            predicted = emitted_spans(text, captured[0], session.values)
                            args_json = response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
                            restored = json.loads(args_json)["document"] == text
                    elapsed = (time.perf_counter() - began) * 1000
                    record = {"id": row["id"], "mode": mode, "language": row["language"],
                              "format": row["format"], "hash": row["hash"], "chars": len(row["text"]),
                              "blocked": blocked, "restoration_ok": restored, "latency_ms": elapsed,
                              "gold_spans": gold, "predicted_spans": predicted,
                              "source_labels": [s["label"] for s in annotations],
                              "no_source_annotations": not row["annotations"], **score(text, gold, predicted)}
                    if blocked:
                        record["error_type"] = error
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    all_records[mode].append(record)
                if (i + 1) % 100 == 0 or i + 1 == len(rows):
                    stream.flush()
                    print(json.dumps({"completed": i + 1, "total": len(rows), "elapsed_s": round(time.monotonic() - start, 1)}), flush=True)
    report = {"manifest": manifest, "elapsed_s": time.monotonic() - start, "modes": {}}
    for mode, records in all_records.items():
        by_label = defaultdict(Counter)
        by_type = defaultdict(Counter)
        for r in records:
            for label, covered in zip(r["source_labels"], r["full_flags"]):
                by_label[label]["gold"] += 1
                by_label[label]["fully_covered"] += covered
            for a, b, kind in r["gold_spans"]:
                by_type[kind]["gold"] += 1
                by_type[kind]["tp"] += [a, b, kind] in r["predicted_spans"] or (a, b, kind) in r["predicted_spans"]
            for _, _, kind in r["predicted_spans"]:
                by_type[kind]["predicted"] += 1
        typed = {k: {**v, "f1": 2 * v["tp"] / (v["gold"] + v["predicted"])} for k, v in by_type.items()}
        report["modes"][mode] = {
            "overall": aggregate(records), "by_source_label": dict(by_label), "by_type": typed,
            "macro_f1": statistics.mean(v["f1"] for v in typed.values()),
            "by_language": {lang: aggregate([r for r in records if r["language"] == lang]) for lang in sorted({r["language"] for r in records})},
            "no_source_annotation_documents": aggregate([r for r in records if r["no_source_annotations"]]) if any(r["no_source_annotations"] for r in records) else None,
        }
    if source_hashes != {p: sha(ROOT / p) for p in source_hashes}:
        raise RuntimeError("Source changed during benchmark; discard this run")
    (output / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({m: r["overall"] for m, r in report["modes"].items()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="data/evaluation/gretel-finance-7b844d1/test-00000-of-00001.parquet")
    parser.add_argument("--output", default="evaluation/reports/model_masking/public_gretel")
    parser.add_argument("--limit", type=int, default=0, help="Smoke run only; 0 runs the complete eligible split")
    asyncio.run(run(parser.parse_args()))
