"""External corpus for a frozen before/after run; explicitly record reuse status.

Restore masking-sources with scripts/evidence.py; pass its selected snapshot directory via --source-root.
Entity-value annotations are projected to every exact occurrence, without guesses.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SHAS = {"test": "c89a6bffa838edb0f2fc6686bf35da4930944519ac97308b0b6e32298594b5b1",
        "validation": "3de64be0dddd0dc8a6a1a1368533cc1efe8a9ec290f87feaa064e4e78a5d22b8"}
REVISION = "e06eb1499ca8d54470f085021cd8e54f9efac7fd"


async def run(args):
    # Namespace package overlay loads only these saved modules; other modules stay current.
    source = Path(args.source_root).resolve()
    sys.path.insert(0, str(source))
    os.environ.update(IT_COPILOT_MASK_MODEL="on", IT_COPILOT_MASK_MODEL_THRESHOLD="0.8",
                      IT_COPILOT_MASK_MODEL_TIMEOUT_S="10", IT_COPILOT_MASK_WORDS="[]",
                      IT_COPILOT_ASTRLINK_DIR=str(ROOT / "data/models/astrlink-guard/7216b6a3b897"))
    import httpx2
    import pyarrow.parquet as pq
    from core.local_pii import detector
    from core.model_masking import MaskingTransport, masking_scope
    from evaluation.masking_benchmark import LABEL_MAP, emitted_spans, score
    from scripts.evaluate_masking_public import PREFIX, aggregate, sha

    mapping = {**LABEL_MAP, "address": "ADDRESS", "account_number": "ACCOUNT",
               "national_id": "ID", "tax_id": "ID", "cvv": "SECRET", "pin": "SECRET",
               "medical_record_number": "ACCOUNT", "health_plan_beneficiary_number": "ACCOUNT",
               "biometric_identifier": "ACCOUNT", "certificate_license_number": "ID",
               "license_plate": "ID", "vehicle_identifier": "ID"}
    excluded = {
        "date_of_birth": "dates disabled; coverage gap", "date": "dates disabled",
        "date_time": "dates disabled", "time": "time disabled", "ipv4": "IP disabled", "ipv6": "IP disabled",
        "customer_id": "business identifier", "employee_id": "business identifier",
        "device_identifier": "business identifier", "unique_identifier": "unspecified business identifier",
        "company_name": "organization", "city": "coarse geography", "postcode": "coarse geography",
        "country": "coarse geography", "state": "coarse geography", "coordinate": "unsupported coordinates",
        "swift_bic": "bank identifier", "url": "URL disabled",
    }
    path = ROOT / f"data/evaluation/gretel-en-e06eb14/{args.split}.parquet"
    assert sha(path) == SHAS[args.split]
    raw = pq.read_table(path).to_pylist()
    prior = pq.read_table(ROOT / "data/evaluation/gretel-finance-7b844d1/test-00000-of-00001.parquet", columns=["generated_text"]).to_pylist()
    seen = {hashlib.sha256(r["generated_text"].encode()).hexdigest() for r in prior}
    if args.split == "validation":
        prior_test = pq.read_table(ROOT / "data/evaluation/gretel-en-e06eb14/test.parquet", columns=["text"]).to_pylist()
        seen.update(hashlib.sha256(r["text"].encode()).hexdigest() for r in prior_test)
    duplicates, rows, counts = [], [], Counter()
    for index, row in enumerate(raw):
        entities = ast.literal_eval(row["entities"])
        digest = hashlib.sha256(row["text"].encode()).hexdigest()
        if digest in seen:
            duplicates.append(index)
            continue
        seen.add(digest)
        gold = set()
        for e in entities:
            assert e["entity"] and e["entity"] in row["text"], "Unprojectable annotation"
            for label in e["types"]:
                counts[label] += 1
                assert label in mapping or label in excluded, label
                if label in mapping:
                    for m in re.finditer(re.escape(e["entity"]), row["text"]):
                        gold.add((m.start() + len(PREFIX), m.end() + len(PREFIX), mapping[label]))
        rows.append((index, PREFIX + row["text"], sorted(gold), digest))
    sources = {str(p.relative_to(source)): sha(p) for p in (source / "core").glob("*.py")
               if p.name in {"local_pii.py", "redaction.py", "model_masking.py", "pii_rules.py"}}
    manifest = {"dataset": "gretelai/gretel-pii-masking-en-v1", "revision": REVISION,
                "split": args.split, "sha256": SHAS[args.split], "raw_documents": len(raw), "documents": len(rows),
                "duplicate_or_prior_corpus_ids": duplicates, "annotation_counts": dict(counts),
                "projection": "all exact occurrences; typed duplicate spans deduplicated",
                "mapping": mapping, "excluded": excluded, "source_root": str(source), "source_sha256": sources,
                "evaluator_sha256": sha(__file__),
                "evaluation_role": args.evaluation_role,
                "notes": ("Previously evaluated corpus; regression evidence, not a new independent holdout"
                          if args.evaluation_role == "regression" else "Frozen holdout; caller must establish no tuning on this split")
                         + "; synthetic, not proven unseen during model pretraining"}
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    model = detector()
    captured = []

    def upstream(request):
        masked = json.loads(request.content)["messages"][0]["content"]
        captured.append(masked)
        return httpx2.Response(200, json={"choices": [{"message": {"tool_calls": [{"function": {
            "name": "inspect", "arguments": json.dumps({"document": masked})}}]}}]})

    records = []
    with (out / "documents.jsonl").open("w") as stream:
        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
            for index, text, gold, digest in rows:
                model.cache.clear()
                captured.clear()
                started = time.perf_counter()
                with masking_scope("holdout", str(index)) as session:
                    response = await client.post("http://fixture/v1/chat/completions", json={"messages": [{"role": "user", "content": text}]})
                    predicted = emitted_spans(text, captured[0], session.values)
                    restored = json.loads(response.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])["document"] == text
                r = {"id": index, "hash": digest, "blocked": False, "restoration_ok": restored,
                     "latency_ms": (time.perf_counter() - started) * 1000,
                     "gold_spans": gold, "predicted_spans": predicted, **score(text, gold, predicted)}
                records.append(r)
                stream.write(json.dumps(r) + "\n")
                if len(records) % 250 == 0:
                    stream.flush()
                    print(f"completed {len(records)}/{len(rows)}", flush=True)
    assert sources == {p: sha(source / p) for p in sources}, "Implementation changed during run"
    report = {"manifest": manifest, "overall": aggregate(records)}
    (out / "metrics.json").write_text(json.dumps(report, indent=2) + "\n")
    print("Completed; metrics saved. Do not inspect holdout until the implementation is frozen.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", default=str(ROOT))
    parser.add_argument("--split", choices=list(SHAS), default="test")
    parser.add_argument("--output", required=True)
    parser.add_argument("--evaluation-role", choices=["regression", "frozen_holdout"], default="regression")
    asyncio.run(run(parser.parse_args()))
