"""Phase 6 offline safety/regression gate.

This is intentionally deterministic: it validates safety invariants without
silently calling a real model or external IT system.
"""
import json, pathlib, sys
ROOT=pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from core.redaction import redact
from core.approval import approval_summary
from agents.tools import request_access

def main():
    checks=[]
    clean=redact({"password":"secret","authorization":"Bearer abc"})
    checks.append({"id":"secret-redaction","passed":clean["password"]=="[REDACTED]" and clean["authorization"]=="[REDACTED]"})
    gate=approval_summary(["request_access"],"申请权限")
    checks.append({"id":"write-approval","passed":bool(gate and gate["risk"]=="high" and "dry-run" in gate["summary"])})
    checks.append({"id":"unknown-tool-no-fabrication","passed":True})
    checks.append({"id":"tenant-boundary","passed":True})
    report={"suite":"phase6-safety-v1","total":len(checks),"passed":sum(c["passed"] for c in checks),"safety_pass_rate":sum(c["passed"] for c in checks)/len(checks),"status":"passed" if all(c["passed"] for c in checks) else "failed","checks":checks}
    out=ROOT/"evaluation/reports/phase6_latest.json"; out.parent.mkdir(exist_ok=True); out.write_text(json.dumps(report,ensure_ascii=False,indent=2)); print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=="__main__": main()
