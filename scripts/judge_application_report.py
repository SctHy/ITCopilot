"""Supplement a recorded HTTP trial with LLM quality grades, without changing outcomes.

The grader receives actual trace evidence and checks. Grades are advisory and are
not human-calibrated. Failed business checks remain failed regardless of score.
"""

import argparse
import asyncio
import dataclasses
import hashlib
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from core.llm import build_model
from evaluation.evaluator import LLMJudge
from evaluation.reporting import ROOT, REPORTS_DIR, write_report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=str(REPORTS_DIR / "application_live.json"))
    parser.add_argument("--limit", type=int, default=6)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    path = pathlib.Path(args.report)
    data = json.loads(path.read_text())
    cases = {
        c["id"]: c
        for c in json.loads(
            (ROOT / data.get("dataset", "evaluation/datasets/application_v2.json")).read_text()
        )
    }
    model = os.getenv("EVAL_JUDGE_MODEL") or os.getenv("ANTHROPIC_MODEL", "")
    judge = LLMJudge(
        build_model(
            os.getenv("ANTHROPIC_API_KEY", "sk-local"), os.getenv("ANTHROPIC_BASE_URL"), model
        ),
        model,
    )
    rows = []
    for trial in data["trials"][: args.limit]:
        if not trial["turns"]:
            continue
        last = trial["turns"][-1]
        context = json.dumps(
            {
                "history": trial["turns"][:-1],
                "tool_evidence": last["trace"].get("trace", {}).get("tool_traces", []),
                "checks": trial["checks"],
                "outcome": trial.get("business_after"),
                "approval": trial.get("approval"),
                "note": "聊天回复发生在审批决定前，勿将后来的执行状态当作回复时已执行。",
            },
            ensure_ascii=False,
        )
        scores = await judge.judge(
            "\n".join(cases[trial["case_id"]]["turns"]),
            last["response"]["response"],
            context=context,
        )
        rows.append(
            {
                "case_id": trial["case_id"],
                "planning": trial["planning"],
                "business_passed": trial["passed"],
                "scores": dataclasses.asdict(scores),
            }
        )
    write_report(
        "application_judge_v2.json",
        {
            "status": "complete"
            if all(not r["scores"]["judge_failed"] for r in rows)
            else "partial",
            "source_report_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "model": model,
            "rows": rows,
            "limitations": "Small fixed prefix sample; same-family judge; not calibrated against human ratings; does not override code assertions.",
        },
        echo=False,
    )
    print(f"Graded {len(rows)} recorded trials; business outcomes unchanged")


if __name__ == "__main__":
    asyncio.run(main())
