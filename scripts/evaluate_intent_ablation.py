"""Frozen calibration/test split, shared source predictions, source-bound calibration.

--live calls the configured real model ONCE per example, then scores all four
strategies using identical source predictions. Without --live only local routes
are measured; fallback is never reported as LLM/fusion quality.
"""

import argparse
import asyncio
import hashlib
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from core.intent_calibration import brier, calibrate, ece, fit_temperatures
from core.intent_recognizer import INTENT_GROUPS, IntentRecognizer, _fallback_vote
from evaluation.reporting import ROOT, DATASETS_DIR, write_report


def classification_metrics(rows):
    labels = sorted({r["expected"] for r in rows})
    f1 = []
    for label in labels:
        tp = sum(r["expected"] == label == r["predicted"] for r in rows)
        fp = sum(r["predicted"] == label != r["expected"] for r in rows)
        fn = sum(r["expected"] == label != r["predicted"] for r in rows)
        f1.append(2 * tp / max(1, 2 * tp + fp + fn))
    return {
        "n": len(rows),
        "accuracy": sum(r["correct"] for r in rows) / max(1, len(rows)),
        "macro_f1": sum(f1) / max(1, len(f1)),
    }


async def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", action="store_true")
    p.add_argument("--install-calibration", action="store_true")
    p.add_argument("--concurrency", type=int, default=3)
    args = p.parse_args()
    load_dotenv(ROOT / ".env")
    os.environ["INTENT_EMBEDDING_PROVIDER"] = "local"
    source = DATASETS_DIR / "intent_v2.json"
    cases = json.loads(source.read_text())
    assert len({r["message"] for r in cases}) == len(cases)
    strategies = ["pattern", "embedding"] + (["llm", "fusion"] if args.live else [])
    recognizer = IntentRecognizer(
        os.getenv("ANTHROPIC_API_KEY", "sk-local"),
        os.getenv("ANTHROPIC_BASE_URL"),
        os.getenv("ANTHROPIC_MODEL", ""),
    )
    gate = asyncio.Semaphore(max(1, args.concurrency))

    async def collect(case):
        async with gate:
            start = time.monotonic()
            pattern = recognizer._pattern(case["message"])
            embedding = await recognizer._embedding_match(case["message"])
            llm = (
                await recognizer._llm(case["message"], None)
                if args.live
                else _fallback_vote("disabled")
            )
            return {
                **case,
                "pattern": pattern,
                "embedding": embedding,
                "llm": llm,
                "source_latency_s": time.monotonic() - start,
            }

    observations = await asyncio.gather(*(collect(case) for case in cases))
    reports, configs = {}, {}
    for strategy in strategies:
        recognizer.strategy = strategy
        rows = []
        for obs in observations:
            prediction, confidence, _ = recognizer.select_vote(
                strategy, obs["llm"], obs["embedding"], obs["pattern"]
            )
            embedding = obs["embedding"] if strategy in {"fusion", "embedding"} else {}
            llm = obs["llm"] if strategy in {"fusion", "llm"} else {}
            rows.append(
                {
                    "id": obs["id"],
                    "split": obs["split"],
                    "expected": obs["expected_intent"],
                    "predicted": prediction.value,
                    "correct": prediction.value == obs["expected_intent"],
                    "confidence": confidence,
                    "source": recognizer.calibration_source(llm, embedding),
                    "group": INTENT_GROUPS[prediction],
                }
            )
        # Fit on calibration rows only; keep a separate fit for each actual source.
        for key in {r["source"] for r in rows}:
            groups = {}
            for row in rows:
                if row["split"] == "calibration" and row["source"] == key:
                    groups.setdefault(row["group"], []).append((row["confidence"], row["correct"]))
            configs[key] = fit_temperatures(groups)
        for row in rows:
            row["calibrated"] = calibrate(
                row["confidence"], configs.get(row["source"], {}).get(row["group"], 1.0)
            )
        test = [r for r in rows if r["split"] == "test"]
        before = [(r["confidence"], r["correct"]) for r in test]
        after = [(r["calibrated"], r["correct"]) for r in test]
        reports[strategy] = {
            **classification_metrics(test),
            "ece_before": ece(before),
            "ece_after": ece(after),
            "brier_before": brier(before),
            "brier_after": brier(after),
            "cases": rows,
        }
    complete = not args.live or all(o["llm"].get("_llm_used") for o in observations)
    report = {
        "suite": "intent-v2-ablation",
        "status": "complete" if complete else "incomplete_model_failures",
        "model": recognizer.model if args.live else None,
        "live": args.live,
        "dataset_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "calibration_n": sum(c["split"] == "calibration" for c in cases),
        "test_n": sum(c["split"] == "test" for c in cases),
        "source_calls": recognizer.metrics(),
        "strategies": reports,
        "observations": observations,
        "limitations": "Authored small dataset, no independent human annotation; shared raw predictions isolate voting effects; group temperatures are high variance.",
    }
    filename = "intent_ablation_live.json" if args.live else "intent_ablation_local.json"
    write_report(filename, report, echo=False)
    print(
        json.dumps(
            {
                "status": report["status"],
                "strategies": {
                    k: {a: b for a, b in v.items() if a != "cases"} for k, v in reports.items()
                },
            },
            ensure_ascii=False,
        )
    )
    if args.install_calibration:
        if not complete:
            raise RuntimeError("Refusing to install calibration from failed model calls")
        # Report first. Installing is explicit, and NEVER chooses a strategy by test scores.
        (ROOT / "evaluation/config/intent_calibration.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "dataset_sha256": report["dataset_sha256"],
                    "fit_split": "calibration",
                    "temperatures_by_source": configs,
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    asyncio.run(main())
