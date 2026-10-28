#!/usr/bin/env python3
"""依次运行各离线评测套件，汇总为一份门禁报告（任一失败则退出码为 1）。"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from evaluation.reporting import REPORTS_DIR, ROOT, status, write_report

# (脚本, 报告文件, 额外环境变量)；意图套件固定关闭 LLM，保证结果可复现。
SUITES = (
    ("evaluate_intents.py", "intent_latest.json", {"IT_INTENT_LLM_ENABLED": "false"}),
    ("evaluate_memory.py", "memory_latest.json", {}),
    ("evaluate_rag.py", "rag_latest.json", {}),
    ("evaluate_planner.py", "planner_latest.json", {}),
    ("evaluate_phase6.py", "phase6_latest.json", {}),
)


def run_suite(script: str, report_file: str, extra_env: dict) -> dict:
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / script)], cwd=ROOT, env={**os.environ, **extra_env}, text=True, capture_output=True)
    path = REPORTS_DIR / report_file
    report = json.loads(path.read_text()) if path.exists() else {}
    return {
        "suite": script.removesuffix(".py"), "exit_code": proc.returncode,
        "status": report.get("status", "failed" if proc.returncode else "unknown"), "report": str(path.relative_to(ROOT)),
    }


def main():
    rows = [run_suite(*suite) for suite in SUITES]
    passed = all(row["exit_code"] == 0 and row["status"] == "passed" for row in rows)
    write_report("all_latest.json", {"suite": "it-copilot-evaluation-gate", "status": status(passed), "all_passed": passed, "suites": rows})
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
