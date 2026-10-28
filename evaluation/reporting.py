"""离线评测脚本的公共工具：固定报告目录与报告写入。"""
from __future__ import annotations

import json
import pathlib
from typing import Any, Dict

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPORTS_DIR = ROOT / "evaluation/reports"
DATASETS_DIR = ROOT / "evaluation/datasets"


def write_report(filename: str, report: Dict[str, Any], *, echo: bool = True) -> pathlib.Path:
    """写入 evaluation/reports/<filename>（UTF-8、缩进 2），并按需打印到 stdout。"""
    path = REPORTS_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8")
    if echo:
        print(text)
    return path


def status(passed: bool) -> str:
    return "passed" if passed else "failed"
