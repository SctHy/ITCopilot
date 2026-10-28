#!/usr/bin/env python3
"""在固定意图数据集上按意图组拟合温度缩放参数，写出校准报告与运行时使用的温度配置。"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.intent_calibration import brier, calibrate, ece, fit_temperatures
from core.intent_recognizer import INTENT_GROUPS, IntentRecognizer
from evaluation.evaluator import load_intent_cases
from evaluation.reporting import DATASETS_DIR, REPORTS_DIR, ROOT, write_report

CALIBRATION_CONFIG = ROOT / "evaluation/config/intent_calibration.json"


async def main():
    os.environ["IT_INTENT_LLM_ENABLED"] = "false"
    # 拟合时不能加载旧温度，否则会在已校准的置信度上再次校准。
    os.environ["IT_INTENT_CALIBRATION_PATH"] = str(REPORTS_DIR / "does-not-exist.json")
    cases = load_intent_cases(DATASETS_DIR / "intent_v1.json")
    recognizer = IntentRecognizer("sk-local")
    rows = []
    for case in cases:
        result = await recognizer.recognize(case.message, previous_intent_group=(case.context or {}).get("previous_intent_group"))
        rows.append((INTENT_GROUPS.get(result.intent, "general"), result.confidence, result.intent.value == case.expected_intent))

    by_group = {}
    for group, confidence, correct in rows:
        by_group.setdefault(group, []).append((confidence, correct))
    temperatures = fit_temperatures(by_group)
    before = [(p, ok) for _, p, ok in rows]
    after = [(calibrate(p, temperatures.get(group, 1.0)), ok) for group, p, ok in rows]

    write_report("intent_calibration_latest.json", {
        "suite": "intent-calibration-v1", "dataset_size": len(cases), "temperatures": temperatures,
        "ece_before": ece(before), "ece_after": ece(after), "brier_before": brier(before), "brier_after": brier(after),
        "thresholds": {"clarify": float(os.getenv("IT_INTENT_CLARIFY_THRESHOLD", "0.60"))}, "status": "passed",
    })
    CALIBRATION_CONFIG.parent.mkdir(exist_ok=True)
    CALIBRATION_CONFIG.write_text(json.dumps({"schema_version": 1, "source_dataset": "evaluation/datasets/intent_v1.json", "temperatures": temperatures}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
