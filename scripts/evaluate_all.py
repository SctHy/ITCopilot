#!/usr/bin/env python3
"""Run the reproducible Phase 2-6 evaluation suites and emit one gate report."""
from __future__ import annotations
import json, os, pathlib, subprocess, sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
SCRIPTS=["evaluate_intents.py","evaluate_memory.py","evaluate_rag.py","evaluate_planner.py","evaluate_phase6.py"]

def main():
    rows=[]
    for name in SCRIPTS:
        env=os.environ.copy()
        if name == "evaluate_intents.py": env["IT_INTENT_LLM_ENABLED"]="false"
        proc=subprocess.run([sys.executable,str(ROOT/"scripts"/name)],cwd=ROOT,env=env,text=True,capture_output=True)
        report_name={"evaluate_intents.py":"intent_latest.json","evaluate_memory.py":"memory_latest.json","evaluate_rag.py":"rag_latest.json","evaluate_planner.py":"planner_latest.json","evaluate_phase6.py":"phase6_latest.json"}[name]
        report_path=ROOT/"evaluation/reports"/report_name
        report=json.loads(report_path.read_text()) if report_path.exists() else {}
        rows.append({"suite":name.removesuffix(".py"),"exit_code":proc.returncode,"status":report.get("status", "failed" if proc.returncode else "unknown"),"report":str(report_path.relative_to(ROOT))})
    passed=all(r["exit_code"]==0 and r["status"] in {"passed","complete"} for r in rows)
    result={"suite":"it-copilot-evaluation-gate","status":"passed" if passed else "failed","all_passed":passed,"suites":rows}
    out=ROOT/"evaluation/reports/all_latest.json"; out.parent.mkdir(exist_ok=True); out.write_text(json.dumps(result,ensure_ascii=False,indent=2)); print(json.dumps(result,ensure_ascii=False,indent=2)); raise SystemExit(0 if passed else 1)
if __name__=="__main__": main()
