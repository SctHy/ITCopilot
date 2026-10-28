"""Bounded planner: turns a request into auditable agent steps; it never calls tools.

Two sources share one validation path (``finalize``):
- ``LLMPlanner``: structured output over the request *content*, so routing does not
  depend on a single intent label ("解释 403" goes to the network agent even when the
  intent classifier says "howto").
- ``build_plan``: keyword rules, used when the LLM planner fails or is disabled.

Both add a ticket step only when the user explicitly asks for a ticket (v3 evaluation:
the old rule appended one to every multi-domain request and created unrequested
ticket approvals).
"""
import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)
AGENT_TYPES = ("triage", "endpoint", "access", "network_software", "ticket")
MAX_PLAN_STEPS = 4


def default_token_budget(steps: int = 3) -> int:
    """Agent tokens allowed before a new wave starts (checked between waves, not mid-step).

    Scales with plan size: measured on deepseek-flash (2026-10-29) one specialist step uses
    roughly 3k-10k agent tokens, so a fixed 12000 (later 30000) stopped three-domain plans
    before their last wave. Override the per-step allowance with IT_COPILOT_PLAN_TOKENS_PER_STEP
    or pin a total with IT_COPILOT_PLAN_TOKEN_BUDGET.
    """
    try:
        if os.getenv("IT_COPILOT_PLAN_TOKEN_BUDGET"):
            return max(1000, int(os.environ["IT_COPILOT_PLAN_TOKEN_BUDGET"]))
        return max(1000, int(os.getenv("IT_COPILOT_PLAN_TOKENS_PER_STEP", "10000")) * max(1, steps))
    except ValueError:
        return 10000 * max(1, steps)


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
    token_budget: int = field(default_factory=default_token_budget)

    def to_dict(self) -> Dict[str, Any]:
        return {"plan_id": self.plan_id, "goal": self.goal, "max_steps": self.max_steps,
                "token_budget": self.token_budget, "steps": [s.to_dict() for s in self.steps]}


def _normalize_text(message: str) -> str:
    """Normalize only matching text; preserve the original goal for display."""
    return str(message or "").casefold()


TICKET_REQUEST = ("工单", "建单", "报修单", "ticket", "记录下来", "跟进单")
ERROR_CODE = re.compile(r"(?<![a-z0-9])(?:vpn-\d{3}|[45]\d{2})(?![0-9])")
ASSET_ID = re.compile(r"dev-\d+")
RISK = {"access": "high", "endpoint": "medium", "ticket": "medium"}


def wants_ticket(message: str) -> bool:
    lower = _normalize_text(message)
    return any(k in lower for k in TICKET_REQUEST) and not any(k in lower for k in ("不要建单", "不要建工单", "不需要建单", "不用建单"))


def finalize(steps: List[PlanStep], message: str, plan_id: str) -> ExecutionPlan:
    """Validate any proposed plan: known agents, unique ids, deps only on earlier steps,
    one step per agent (except ticket), ticket only if asked and depending on all others."""
    unique, seen_agents, ids = [], set(), set()
    for step in steps:
        if step.agent_type not in AGENT_TYPES or step.step_id in ids:
            continue
        if step.agent_type == "ticket" and not wants_ticket(message):
            continue
        if step.agent_type in seen_agents and step.agent_type != "ticket":
            continue
        seen_agents.add(step.agent_type)
        ids.add(step.step_id)
        unique.append(step)
    if len(unique) > 1:
        unique = [s for s in unique if s.agent_type != "triage"] or unique[:1]
    others = [s for s in unique if s.agent_type != "ticket"]
    ticket = [s for s in unique if s.agent_type == "ticket"][:1]
    if not others and not ticket:
        others = [PlanStep("triage", "澄清请求并完成安全分流", "triage", [])]
    ordered = others[: MAX_PLAN_STEPS - len(ticket)]
    final = [PlanStep(s.step_id, s.title, s.agent_type, [], s.parallelizable, RISK.get(s.agent_type, "low")) for s in ordered]
    if ticket:
        final.append(PlanStep(ticket[0].step_id, ticket[0].title, "ticket", [s.step_id for s in final], risk="medium"))
    return ExecutionPlan(plan_id, str(message or "")[:500], final, token_budget=default_token_budget(len(final)))


def build_plan(message: str, plan_id: str) -> ExecutionPlan:
    """Keyword fallback planner."""
    lower = _normalize_text(message)
    steps: List[PlanStep] = []
    if any(k in lower for k in ("权限", "账号", "密码", "mfa", "解锁", "锁定")):
        steps.append(PlanStep("access", "核验身份与权限申请信息", "access", []))
    if any(k in lower for k in ("电脑", "设备", "笔记本", "终端", "新机", "资产", "黑屏", "蓝屏", "显示器", "键盘")) or ASSET_ID.search(lower):
        steps.append(PlanStep("endpoint", "准备或核验终端设备信息", "endpoint", []))
    if any(k in lower for k in ("vpn", "网络", "wifi", "无线", "错误码", "报错", "服务状态")) or ERROR_CODE.search(lower):
        steps.append(PlanStep("network", "检查网络、错误码或服务状态", "network_software", []))
    if any(k in lower for k in ("软件", "安装", "许可证", "邮件")):
        steps.append(PlanStep("software", "核对软件安装与许可证要求", "network_software", []))
    if wants_ticket(message):
        steps.append(PlanStep("ticket", "汇总证据并准备 IT 工单", "ticket", []))
    return finalize(steps, message, plan_id)


class _PlannedStep(BaseModel):
    step_id: str = Field(description="短英文标识，如 network、endpoint")
    agent_type: Literal["triage", "endpoint", "access", "network_software", "ticket"]
    title: str = Field(description="该步骤要完成的事，一句话")


class _PlanOutput(BaseModel):
    steps: List[_PlannedStep] = Field(min_length=1, max_length=MAX_PLAN_STEPS)


PLANNER_PROMPT = """你是企业 IT 服务台的任务规划器，只负责把用户请求拆成交给专职 Agent 的步骤，必须调用 make_plan。
可用 Agent 与职责：
- endpoint：设备/终端/资产查询与硬件故障（黑屏、蓝屏、键盘、显示器、磁盘、DEV-xxx 资产号）
- network_software：网络、VPN、错误码解释（如 401/403/500、VPN-691）、服务状态、软件安装与许可证、邮件
- access：账号与权限：权限申请、密码重置、账号解锁、MFA
- ticket：创建或查询工单。只有用户明确要求“建工单/记录工单/提交工单”或查询工单号时才加入
- triage：打招呼、闲聊、超出范围或无法判断领域的请求
规则：用户提到几个独立问题就分几步，每个领域最多一步；纯咨询不要加 ticket；不要为没提到的领域加步骤；最多 {max_steps} 步。
用户请求：{message}
最近对话：{history}"""


class LLMPlanner:
    def __init__(self, llm):
        from pydantic_ai import Agent
        from pydantic_ai.output import ToolOutput

        self.llm = llm
        self._agent = Agent(output_type=ToolOutput(_PlanOutput, name="make_plan", description="输出交给专职 Agent 的步骤列表。"), name="planner")
        self.metrics = {"llm_plans": 0, "fallback_plans": 0, "last_error": None}

    async def plan(self, message: str, plan_id: str, history=None) -> ExecutionPlan:
        from pydantic_ai.settings import ModelSettings

        prompt = PLANNER_PROMPT.format(max_steps=MAX_PLAN_STEPS, message=message, history=json.dumps((history or [])[-4:], ensure_ascii=False))
        try:
            run = self._agent.run(prompt, model=self.llm, model_settings=ModelSettings(max_tokens=800, temperature=0.))
            output = (await asyncio.wait_for(run, timeout=float(os.getenv("IT_COPILOT_PLANNER_TIMEOUT_S", "30")))).output
        except Exception as exc:
            self.metrics["fallback_plans"] += 1
            self.metrics["last_error"] = type(exc).__name__
            logger.info("LLM planner fallback to rules: %s", type(exc).__name__)
            return build_plan(message, plan_id)
        self.metrics["llm_plans"] += 1
        steps = [PlanStep(s.step_id, s.title, s.agent_type, []) for s in output.steps]
        return finalize(steps, message, plan_id)


def is_complex_request(message: str) -> bool:
    text = str(message or "")
    lower = _normalize_text(text)
    markers = ("并且", "同时", "另外", "以及", "先…再", "准备新", "权限")
    domains = sum(any(word in lower for word in group) for group in (
        ("权限", "账号", "密码", "mfa"), ("电脑", "设备", "笔记本", "终端"), ("vpn", "wifi", "网络"), ("软件", "安装", "许可证")))
    return (domains >= 2 and any(k in lower for k in ("同时", "并且", "另外", "分别", "以及"))) or sum(marker in lower for marker in markers) >= 2 or ("权限" in lower and any(k in lower for k in ("电脑", "设备", "软件")))
