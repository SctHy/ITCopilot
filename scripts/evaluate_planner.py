"""Offline Phase 5 planner regression and budget checks."""
import json, pathlib, sys
ROOT=pathlib.Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from core.planner import build_plan, is_complex_request

CASES=[
    "申请 GitLab 权限并准备一台新电脑",
    "VPN 连不上并创建工单",
    "重置密码并绑定 MFA",
    "查询工单状态",
    "软件安装和许可证咨询",
]
def main():
    rows=[]
    for i,text in enumerate(CASES):
        plan=build_plan(text,f"eval-{i}")
        rows.append({"message":text,"complex":is_complex_request(text),"steps":len(plan.steps),"bounded":len(plan.steps)<=plan.max_steps,"has_trace_fields":all(s.step_id and s.agent_type for s in plan.steps)})
    total=len(rows); passed=sum(r["bounded"] and r["has_trace_fields"] for r in rows)
    report={"suite":"planner-v1","total":total,"plan_validity":round(passed/total,4),"max_steps":6,"parallel_ready":sum(r["steps"]>1 for r in rows),"status":"passed" if passed==total else "failed","cases":rows}
    out=ROOT/"evaluation/reports/planner_latest.json"; out.parent.mkdir(exist_ok=True); out.write_text(json.dumps(report,ensure_ascii=False,indent=2)); print(json.dumps(report,ensure_ascii=False,indent=2))
if __name__=="__main__": main()
