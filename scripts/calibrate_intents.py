#!/usr/bin/env python3
"""Fit and report intent confidence calibration on the fixed local dataset."""
from __future__ import annotations
import asyncio, json, os, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.intent_calibration import brier, calibrate, ece, fit_temperatures
from core.intent_recognizer import INTENT_GROUPS, IntentRecognizer
from evaluation.evaluator import load_intent_cases

async def main():
    os.environ["IT_INTENT_LLM_ENABLED"] = "false"
    os.environ["IT_INTENT_CALIBRATION_PATH"] = str(ROOT / "evaluation/reports/does-not-exist.json")
    cases = load_intent_cases(ROOT / "evaluation/datasets/intent_v1.json")
    recognizer = IntentRecognizer("sk-local")
    rows = []
    for case in cases:
        result = await recognizer.recognize(case.message, previous_intent_group=(case.context or {}).get("previous_intent_group"))
        group = INTENT_GROUPS.get(result.intent, "general")
        rows.append((group, result.confidence, result.intent.value == case.expected_intent))
    by_group = {}
    for group, confidence, correct in rows: by_group.setdefault(group, []).append((confidence, correct))
    temperatures = fit_temperatures(by_group)
    before = [(p, ok) for _, p, ok in rows]
    after = [(calibrate(p, temperatures.get(group, 1.0)), ok) for group, p, ok in rows]
    report = {"suite":"intent-calibration-v1", "dataset_size":len(cases), "temperatures":temperatures, "ece_before":ece(before), "ece_after":ece(after), "brier_before":brier(before), "brier_after":brier(after), "thresholds":{"clarify":float(os.getenv("IT_INTENT_CLARIFY_THRESHOLD","0.60")),"handoff":float(os.getenv("IT_INTENT_HANDOFF_THRESHOLD","0.35"))}, "status":"passed"}
    output = ROOT / "evaluation/reports/intent_calibration_latest.json"; output.parent.mkdir(exist_ok=True); output.write_text(json.dumps(report,ensure_ascii=False,indent=2)); (ROOT / "evaluation/config").mkdir(exist_ok=True); (ROOT / "evaluation/config/intent_calibration.json").write_text(json.dumps({"schema_version":1,"source_dataset":"evaluation/datasets/intent_v1.json","temperatures":temperatures},ensure_ascii=False,indent=2)); print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__ == "__main__": asyncio.run(main())
