#!/usr/bin/env python3
"""在线端到端对话评测（LLM-as-Judge）：真实调用 Agent 与评分模型，会消耗 token，不属于 evaluate_all 门禁。

  python scripts/evaluate_dialogs.py                 # 内置 3 条对话用例
  python scripts/evaluate_dialogs.py --with-intents  # 额外跑 LLM 意图识别

评分模型默认与 ANTHROPIC_MODEL 相同，可用 EVAL_JUDGE_MODEL 单独指定；基线保存在 evaluation/reports/dialog_baseline.json，
用于后续运行的回归检测（只有全部用例评分成功时才更新基线）。
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from evaluation.reporting import REPORTS_DIR, ROOT, write_report

load_dotenv(ROOT / ".env")

from agents.agent_orchestrator import AgentOrchestrator
from evaluation.evaluator import DEFAULT_DIALOG_CASES, EndToEndEvaluator, load_intent_cases


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--with-intents", action="store_true", help="同时运行 LLM 意图识别评测")
    args = parser.parse_args()
    llm = {"api_key": os.getenv("ANTHROPIC_API_KEY", "sk-local"), "base_url": os.getenv("ANTHROPIC_BASE_URL") or None, "model": os.getenv("ANTHROPIC_MODEL", "deepseek-chat")}
    orchestrator = AgentOrchestrator(**llm)
    evaluator = EndToEndEvaluator(orchestrator, orchestrator._intent_recognizer, **llm, baseline_path=str(REPORTS_DIR / "dialog_baseline.json"))
    report = await evaluator.run(
        intent_cases=load_intent_cases() if args.with_intents else None,
        dialog_cases=DEFAULT_DIALOG_CASES,
        progress=lambda done, total: print(f"[{done}/{total}]", file=sys.stderr),
    )
    write_report("dialog_latest.json", {**dataclasses.asdict(report), "status": report.status})
    return 0 if report.status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
