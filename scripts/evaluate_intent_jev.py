#!/usr/bin/env python3
"""用 Jev（TypeSafe System One）跑意图难例集，并与已有的 LLM 报告逐项对比。

走线上的 IntentRecognizer，阈值设为 0（Jev 的答案一律采用），只有 Jev 调用失败时才会转 LLM，失败次数见 jev_errors。
需要 TYPESAFE_API_KEY（可写在 .env）。每次运行都会调用付费 API：156 条 × --runs 次。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from typing import Any, Dict, List

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from agents.intent.recognizer import IntentRecognizer
from core.jev import JevClient
from evaluation.evaluator import IntentEvaluator, load_intent_cases
from evaluation.reporting import DATASETS_DIR, REPORTS_DIR


def ece(cases: List[Dict[str, Any]], bins: int = 10) -> float:
    """期望校准误差：按置信度分桶，|平均置信度 - 准确率| 的加权和。"""
    total, buckets = len(cases), [[] for _ in range(bins)]
    for item in cases:
        buckets[min(bins - 1, int(float(item["confidence"]) * bins))].append(item)
    return round(sum(len(b) / total * abs(sum(float(i["confidence"]) for i in b) / len(b) - sum(i["correct"] for i in b) / len(b)) for b in buckets if b), 4)


def split_row(report: Dict[str, Any], split: str) -> str:
    cases = [c for c in report["cases"] if c["split"] == split]
    return f"{sum(c['correct'] for c in cases)}/{len(cases)}（严格 {sum(c['strict_correct'] for c in cases)}）"


def summarize(name: str, report: Dict[str, Any]) -> None:
    lat = report["latency_s"]
    urgency = report.get("urgency_accuracy") or {}
    print(f"{name:<10} calibration {split_row(report, 'calibration'):<14} test {split_row(report, 'test'):<14} "
          f"p50 {lat['p50']:.3f}s p95 {lat['p95']:.3f}s  ECE {ece(report['cases']):.3f}  紧急度 {urgency.get('correct', '-')}/{urgency.get('n', '-')}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=str(DATASETS_DIR / "intent_v3.json"))
    parser.add_argument("--baseline", default=str(REPORTS_DIR / "intent_v3_llm.json"))
    parser.add_argument("--output", default=str(REPORTS_DIR / "intent_v3_jev.json"))
    parser.add_argument("--model", default=os.getenv("TYPESAFE_MODEL", "jev-latest"))
    parser.add_argument("--runs", type=int, default=2, help="重复次数，用于观察结果是否稳定")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条，先小规模确认接口可用")
    args = parser.parse_args()
    if not os.getenv("TYPESAFE_API_KEY"):
        print("缺少 TYPESAFE_API_KEY：请在 https://console.typesafe.ai/keys 申请后写入 .env")
        return 2

    cases = load_intent_cases(args.dataset)
    cases = cases[:args.limit] if args.limit else cases
    os.environ["IT_INTENT_JEV_THRESHOLD"] = "0"
    jev = JevClient(os.environ["TYPESAFE_API_KEY"], model=args.model, timeout_s=30, retries=2)
    runs = []
    try:
        for _ in range(args.runs):
            # 每轮新建识别器，避免进程内意图缓存让第二轮直接命中。
            recognizer = IntentRecognizer(os.getenv("OPENAI_API_KEY", "sk-local"), os.getenv("OPENAI_BASE_URL") or None, os.getenv("OPENAI_MODEL", ""), jev=jev)
            runs.append(await IntentEvaluator(recognizer).evaluate(cases))
    finally:
        await jev.aclose()

    report = {**runs[-1], "suite": "intent-v3-jev", "model": args.model,
              "runs": [{k: run[k] for k in ("accuracy", "strict_accuracy", "by_split", "latency_s", "jev_calls", "jev_errors", "jev_input_tokens")} for run in runs]}
    output = pathlib.Path(args.output)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if runs[-1]["jev_errors"]:
        print(f"WARNING: {runs[-1]['jev_errors']} 次 Jev 调用失败（{runs[-1]['jev_last_error']}），这些用例由 LLM 兜底。")
    print(f"每轮 Jev 输入 tokens {runs[-1]['jev_input_tokens']}")
    for i, run in enumerate(runs, 1):
        summarize(f"jev#{i}", run)
    baseline_path = pathlib.Path(args.baseline)
    if not baseline_path.exists() or args.limit:
        return 0
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    summarize("llm", baseline)

    print("\n分维度准确率（jev 最后一轮 vs llm）")
    for tag, row in report["by_tag"].items():
        base = baseline["by_tag"].get(tag, {})
        print(f"  {tag:<16} jev {row['correct']:>3}/{row['n']:<3}  llm {base.get('correct', '-'):>3}/{base.get('n', '-')}")
    by_id = {c["id"]: c for c in baseline["cases"]}
    diffs = [(c, by_id[c["id"]]) for c in report["cases"] if c["id"] in by_id and c["correct"] != by_id[c["id"]]["correct"]]
    print(f"\n两者对错不一致的用例（{len(diffs)}）")
    for jev, llm in diffs:
        print(f"  {jev['id']:<18} {jev['split']:<11} 期望 {jev['expected']:<20} jev {jev['predicted']:<20} llm {llm['predicted']:<20} {jev['message'][:30]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
