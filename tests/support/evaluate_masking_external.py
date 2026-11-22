"""New external corpora, frozen rules/model, original spans and real HTTP masking.

Prepare writes selection/quality/source manifests before any inference.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parents[2]
# Install the frozen core overlay before importing any evaluator that imports core.
_source_parser = argparse.ArgumentParser(add_help=False)
_source_parser.add_argument("--source-root", type=Path, default=ROOT)
SOURCE_ROOT = _source_parser.parse_known_args()[0].source_root.resolve()
from tests.support.external_pii import NEMOTRON_EXCLUDED, NEMOTRON_MAP, ZH_MAP, digest, select_rows, validate_spans
from tests.support.frozen import frozen_input
from tests.support.masking_benchmark import emitted_spans, score
from tests.support.evaluate_masking_public import PREFIX, aggregate, sha

SPECS = {
    "nemotron": {"repository": "nvidia/Nemotron-PII", "revision": "b70ffaf5ff39e079776134c5bf4381f00a9fd1ed",
                 "file": "data/tests/support/nemotron-b70ffaf/test.parquet", "split": "test", "limit": 10000,
                 "sha256": "1a4b0512ecb5370f0992d29d0f9c07351e6de13f0d7ea33bb18cecb984780247", "license": "CC-BY-4.0"},
    "zh_formal": {"repository": "wan9yu/pii-bench-zh", "revision": "c350b94897af668517ff5de237d89f2ce2eaa6f0",
                  "file": "data/tests/support/pii-bench-zh/formal.jsonl", "split": "formal", "limit": 0, "license": "Apache-2.0",
                  "sha256": "bcaa54909fd69c6737b8e442c4d740ce1bf5b95b7d4af70b2f2692010e1ff7a5"},
    "zh_chat": {"repository": "wan9yu/pii-bench-zh", "revision": "c350b94897af668517ff5de237d89f2ce2eaa6f0",
                "file": "data/tests/support/pii-bench-zh/chat.jsonl", "split": "chat", "limit": 0, "license": "Apache-2.0",
                "sha256": "4ddacd78dfac49b723032de5e1c29c432a44049245e03a8ea960c86bbf3b765c"},
}
SPECS["nemotron_unseen_v3"] = {**SPECS["nemotron"], "limit": 5000}
SOURCES = ["src/it_copilot/agent/masking/redaction.py", "src/it_copilot/agent/masking/pii_rules.py", "src/it_copilot/agent/masking/local_pii.py", "src/it_copilot/agent/masking/model_masking.py",
           "tests/support/masking_benchmark.py", "tests/support/external_pii.py",
           "tests/support/frozen.py", "tests/support/fixtures/manifest.json", "tests/support/fixtures/masking-prior.json",
           "tests/support/evaluate_masking_external.py", "tests/support/evaluate_masking_public.py"]


def source_hashes():
    import importlib
    for name in ("redaction", "pii_rules", "local_pii", "model_masking"):
        module = importlib.import_module("it_copilot.agent.masking." + name)
        assert Path(module.__file__).resolve() == SOURCE_ROOT / "src/it_copilot/agent/masking" / (name + ".py"), "Wrong core implementation loaded"
    return {p: sha((SOURCE_ROOT if p.startswith("src/it_copilot/agent/masking/") else ROOT) / p) for p in SOURCES}


def prior_hashes():
    frozen = json.loads(frozen_input("masking-prior").read_text())
    seen = set(frozen["previous_document_hashes"])
    # Cross-check the two new sources and both Chinese subsets, not just old data.
    for name in ["formal", "chat"]:
        path = ROOT / f"data/tests/support/pii-bench-zh/{name}.jsonl"
        for line in path.read_text().splitlines():
            r = json.loads(line)
            yield name, digest(r["text"])
    yield "previous", seen


def prepare(dataset, output, evaluation_role=None):
    spec = SPECS[dataset]
    path = ROOT / spec["file"]
    if "sha256" in spec and sha(path) != spec["sha256"]:
        raise ValueError("Frozen dataset checksum differs")
    is_nv = dataset.startswith("nemotron")
    if is_nv:
        import pyarrow.parquet as pq
        source = pq.read_table(path).to_pylist()
        rows = [{"id": r["uid"], "source_index": i, "text": r["text"], "spans": r["spans"],
                 "group": r["document_format"], "locale": r["locale"], "domain": r["domain"]} for i, r in enumerate(source)]
    else:
        source = [json.loads(line) for line in path.read_text().splitlines()]
        rows = [{"id": r["id"], "source_index": i, "text": r["text"], "group": spec["split"],
                 "spans": [{**s, "label": s["type"]} for s in r["entities"]]} for i, r in enumerate(source)]
    seen = set()
    for name, value in prior_hashes():
        if name == "previous":
            seen.update(value)
        elif is_nv or (dataset == "zh_chat" and name == "formal"):
            seen.add(value)
    prior_selection = None
    if dataset == "nemotron_unseen_v3":
        prior_selection = json.loads(frozen_input("masking-prior").read_text())
        seen.update(prior_selection["nemotron_10000_hashes"])
    selected, duplicates, unique = select_rows(rows, seen, spec["limit"])
    mapping, excluded = (NEMOTRON_MAP, NEMOTRON_EXCLUDED) if is_nv else (ZH_MAP, {})
    ready, invalid, labels, quality = [], [], Counter(), Counter()
    for row in selected:
        try:
            spans = ast.literal_eval(row["spans"]) if is_nv else row["spans"]
            for s in spans:
                if s["label"] not in mapping and s["label"] not in excluded:
                    raise RuntimeError("Unmapped source label: " + s["label"])
            original, counts = validate_spans(row["text"], spans, allow_case_only=is_nv)
        except (ValueError, SyntaxError, TypeError, KeyError) as error:
            invalid.append({"id": row["id"], "source_index": row["source_index"], "reason": str(error)[:100]})
            continue
        quality.update(counts)
        labels.update(label for _, _, label in original)
        gold = sorted(set((a + len(PREFIX), b + len(PREFIX), mapping[k]) for a, b, k in original if k in mapping))
        ready.append({k: v for k, v in row.items() if k != "spans"} | {
            "text": PREFIX + row["text"], "gold_spans": gold,
            "all_spans": [(a + len(PREFIX), b + len(PREFIX), k) for a, b, k in original]})
    manifest = {**spec, "sha256": sha(path), "raw_documents": len(rows), "unique_after_prior_dedup": unique,
                "selection": "salted SHA256 order; sample chosen before annotation checks; invalid documents not replaced",
                "selected_ids": [r["id"] for r in selected], "selected_documents": len(selected),
                "duplicate_document_ids": duplicates, "quarantined_documents": invalid,
                "documents": len(ready), "annotation_counts": dict(labels), "annotation_quality": dict(quality),
                "mapping": mapping, "excluded": excluded, "model_mode": "off/on", "model_threshold": .8,
                "model_revision": "7216b6a3b897ef903d4b1ba367ae6fb95b0fa5e4",
                "backend": "ONNX Runtime CPU INT8, warm model, per-document cache cleared; MockTransport",
                "evaluation_role": evaluation_role or ("new 5000-document holdout, excluded prior 10000; inspect only after implementation freeze; not proven unseen by pretraining"
                                    if prior_selection else "regression corpus previously inspected for rule development"),
                "excluded_prior_selection_sha256": prior_selection["sources"]["nemotron_selection"]["sha256"] if prior_selection else None,
                "dedup_fixture_sha256": sha(frozen_input("masking-prior")),
                "source_root": str(SOURCE_ROOT), "source_sha256": source_hashes()}
    output.mkdir(parents=True, exist_ok=True)
    (output / "prepared.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in ready))
    manifest["prepared_sha256"] = sha(output / "prepared.jsonl")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


async def run(output):
    import httpx2
    from it_copilot.agent.masking.local_pii import detector
    from it_copilot.agent.masking.model_masking import MaskingError, MaskingTransport, masking_scope
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["source_sha256"] == source_hashes(), "Source changed after freeze"
    assert sha(output / "prepared.jsonl") == manifest["prepared_sha256"]
    rows = [json.loads(line) for line in (output / "prepared.jsonl").read_text().splitlines()]
    os.environ.update(IT_COPILOT_MASK_MODEL_THRESHOLD="0.8", IT_COPILOT_MASK_MODEL_TIMEOUT_S="10", IT_COPILOT_MASK_WORDS="[]",
                      IT_COPILOT_ASTRLINK_DIR=str(ROOT / "data/models/astrlink-guard/7216b6a3b897"))
    model, captured, records = detector(), [], defaultdict(list)

    def upstream(request):
        value = json.loads(request.content)["messages"][0]["content"]
        captured.append(value)
        return httpx2.Response(200, json={"choices": [{"message": {"tool_calls": [{"function": {
            "name": "inspect", "arguments": json.dumps({"text": value})}}]}}]})

    with (output / "documents.jsonl").open("w") as stream:
        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
            for i, row in enumerate(rows):
                text, gold = row["text"], [tuple(s) for s in row["gold_spans"]]
                for mode in ("off", "on"):
                    os.environ["IT_COPILOT_MASK_MODEL"] = mode
                    model.cache.clear()
                    captured.clear()
                    start, blocked, restored, predicted = time.perf_counter(), False, False, []
                    with masking_scope("external-benchmark", str(row["id"]) + mode) as session:
                        try:
                            response = await client.post("http://fixture/v1/chat/completions", json={"messages": [{"role": "user", "content": text}]})
                        except MaskingError:
                            blocked = True
                        else:
                            predicted = emitted_spans(text, captured[0], session.values)
                            restored = json.loads(response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])["text"] == text
                    result = {k: row[k] for k in ("id", "source_index", "hash", "group")}
                    all_score = score(text, [tuple(s) for s in row["all_spans"]], predicted)
                    result.update(mode=mode, blocked=blocked, restoration_ok=restored, sent=bool(captured),
                                  latency_ms=(time.perf_counter()-start)*1000, gold_spans=gold, predicted_spans=predicted,
                                  all_source_entities=all_score["gold"], all_source_fully_covered=all_score["fully_covered"],
                                  no_source_annotations=not row["all_spans"], **score(text, gold, predicted))
                    stream.write(json.dumps(result) + "\n")
                    records[mode].append(result)
                if (i+1) % 250 == 0 or i+1 == len(rows):
                    stream.flush()
                    print(f"completed {i+1}/{len(rows)}", flush=True)
    report = {"manifest": manifest, "modes": {}}
    for mode, values in records.items():
        by_type = defaultdict(Counter)
        for r in values:
            for (_, _, kind), flag in zip(r["gold_spans"], r["full_flags"], strict=True):
                by_type[kind]["gold"] += 1
                by_type[kind]["fully_covered"] += flag
        total_all = sum(r["all_source_entities"] for r in values)
        report["modes"][mode] = {
            "overall": aggregate(values), "by_type": dict(by_type),
            "by_group": {g: aggregate([r for r in values if r["group"] == g]) for g in sorted({r["group"] for r in values})},
            "all_source_annotations": {"gold": total_all, "fully_covered": sum(r["all_source_fully_covered"] for r in values),
                                       "note": "Broader than current policy; includes dates and other sensitive attributes"},
            "zero_source_annotation_documents": aggregate([r for r in values if r["no_source_annotations"]]) if any(r["no_source_annotations"] for r in values) else None,
        }
    assert manifest["source_sha256"] == source_hashes(), "Source changed during run"
    (output / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("Finished, frozen sources verified", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=list(SPECS), required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--run-prepared", action="store_true")
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--evaluation-role", help="Explicitly record reuse after a holdout informs a repair")
    args = parser.parse_args()
    if not args.run_prepared:
        manifest = prepare(args.dataset, args.output, args.evaluation_role)
        print({k: manifest[k] for k in ("raw_documents", "selected_documents", "documents", "annotation_quality")}, flush=True)
    if not args.prepare_only:
        asyncio.run(run(args.output))
