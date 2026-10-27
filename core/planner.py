"""安全优先的 Phase 5 轻量 Planner。

Planner 只负责把明显的复合 IT 请求拆成可审计步骤；它不直接调用工具。
生产环境可以将规则替换为受约束的 LLM planner，但仍需通过同一份 Plan 校验。
"""
from dataclasses import dataclass, asdict
from typing import Any, Dict, List


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    title: str
    agent_type: str
    depends_on: List[str]
    parallelizable: bool = True
    risk: str = "low"
    status: str = "pending"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    goal: str
    steps: List[PlanStep]
    max_steps: int = 6
    token_budget: int = 12000

    def to_dict(self) -> Dict[str, Any]:
        return {"plan_id": self.plan_id, "goal": self.goal, "max_steps": self.max_steps,
                "token_budget": self.token_budget, "steps": [s.to_dict() for s in self.steps]}


def _normalize_text(message: str) -> str:
    """Normalize only matching text; preserve the original goal for display."""
    return str(message or "").casefold()


def build_plan(message: str, plan_id: str) -> ExecutionPlan:
    text = str(message or "")
    # casefold() makes English keywords case-insensitive while preserving the
    # original text for the user-visible plan goal.
    lower = _normalize_text(text)
    steps: List[PlanStep] = []
    if any(k in lower for k in ("权限", "账号", "密码", "mfa", "解锁")):
        steps.append(PlanStep("access", "核验身份与权限申请信息", "access", [], risk="high"))
    if any(k in lower for k in ("电脑", "设备", "笔记本", "终端", "新机")):
        steps.append(PlanStep("endpoint", "准备或核验终端设备信息", "endpoint", [], risk="medium"))
    if any(k in lower for k in ("vpn", "网络", "wifi", "无线")):
        steps.append(PlanStep("network", "检查网络或 VPN 状态", "network_software", []))
    if any(k in lower for k in ("软件", "安装", "许可证", "邮件")):
        steps.append(PlanStep("software", "核对软件安装与许可证要求", "network_software", []))
    if any(k in lower for k in ("工单", "升级", "跟进")) or len(steps) > 1:
        deps = [s.step_id for s in steps if s.risk != "high"]
        steps.append(PlanStep("ticket", "汇总证据并准备 IT 工单", "ticket", deps, risk="medium"))
    if not steps:
        steps = [PlanStep("triage", "澄清请求并完成安全分流", "triage", [])]
    # Duplicate agent steps are merged to keep the plan bounded and replayable.
    unique, seen = [], set()
    for step in steps:
        if step.agent_type in seen and step.agent_type != "ticket":
            continue
        seen.add(step.agent_type); unique.append(step)
    return ExecutionPlan(plan_id, text[:500], unique[:6])


def is_complex_request(message: str) -> bool:
    text = str(message or "")
    lower = _normalize_text(text)
    markers = ("并且", "同时", "另外", "以及", "先…再", "准备新", "权限")
    return sum(marker in lower for marker in markers) >= 2 or ("权限" in lower and any(k in lower for k in ("电脑", "设备", "软件")))
