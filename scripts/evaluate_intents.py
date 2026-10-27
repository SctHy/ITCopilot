#!/usr/bin/env python3
"""Run the versioned intent suite without starting the web stack.

By default the suite uses the real tool-use recognizer. Set
IT_INTENT_LLM_ENABLED=false for a deterministic local fallback smoke test.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", os.environ["NO_PROXY"])

from core.intent_recognizer import IntentRecognizer
from evaluation.evaluator import IntentEvaluator, load_intent_cases


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(ROOT / "evaluation/datasets/intent_v1.json"))
    parser.add_argument("--output", default=str(ROOT / "evaluation/reports/intent_latest.json"))
    args = parser.parse_args()
    cases = load_intent_cases(args.dataset)
    recognizer = IntentRecognizer(
        os.getenv("ANTHROPIC_API_KEY", "sk-local"),
        os.getenv("ANTHROPIC_BASE_URL") or None,
        os.getenv("ANTHROPIC_MODEL", "deepseek-chat"),
    )
    metrics = await IntentEvaluator(recognizer).evaluate(cases)
    metrics["status"] = "passed" if metrics.get("accuracy", 0) >= 0.75 else "failed"
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = {k: metrics[k] for k in ("total", "correct", "accuracy", "macro_f1", "llm_calls", "llm_success", "fallback_calls", "fallback_disabled", "fallback_errors", "last_error", "llm_only_accuracy", "llm_only_total", "fallback_accuracy", "fallback_total")}
    print(json.dumps(summary, ensure_ascii=False))
    if metrics.get("llm_calls", 0) == 0:
        print("WARNING: 本次没有调用 LLM；结果是规则兜底基线，不代表真实 Tool Use 质量。")
    elif metrics.get("llm_success", 0) == 0:
        print(f"WARNING: LLM 调用全部失败，已使用 fallback；失败类型={metrics.get('last_error')}")
    return 0 if metrics["accuracy"] >= 0.75 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
