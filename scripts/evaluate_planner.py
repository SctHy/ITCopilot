"""Phase 5 Planner 离线回归：复合请求拆分、步数上限与 trace 字段完整性。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core.planner import build_plan, is_complex_request
from evaluation.reporting import status, write_report

CASES = [
    "申请 GitLab 权限并准备一台新电脑",
    "VPN 连不上并创建工单",
    "重置密码并绑定 MFA",
    "查询工单状态",
    "软件安装和许可证咨询",
]


def main():
    rows, max_steps = [], 0
    for i, text in enumerate(CASES):
        plan = build_plan(text, f"eval-{i}")
        max_steps = plan.max_steps
        rows.append({
            "message": text, "complex": is_complex_request(text), "steps": len(plan.steps),
            "bounded": len(plan.steps) <= plan.max_steps, "has_trace_fields": all(s.step_id and s.agent_type for s in plan.steps),
        })
    valid = sum(r["bounded"] and r["has_trace_fields"] for r in rows)
    write_report("planner_latest.json", {
        "suite": "planner-v1", "total": len(rows), "plan_validity": round(valid / len(rows), 4), "max_steps": max_steps,
        "parallel_ready": sum(r["steps"] > 1 for r in rows), "status": status(valid == len(rows)), "cases": rows,
    })


if __name__ == "__main__":
    main()
