"""运行状态：健康检查、Skill、监控、指标、请求 trace 与离线评测报告。"""
import asyncio
import json
import os
import pathlib
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from api import state
from api.deps import current_principal, forbid, require_admin, require_roles
from core.identity import ROLE_AUDITOR, ROLE_IT_ADMIN, Principal

_ROOT = pathlib.Path(__file__).resolve().parents[2]

router = APIRouter()


@router.get("/health")
async def health():
    agents = state.orchestrator.agent_types() if state.orchestrator else []
    return {"status": "ok", "service": "IT-Copilot", "port": int(os.getenv("IT_COPILOT_PORT", "8001")), "agents": agents}


@router.get("/skills")
async def skills():
    return state.skills.summary() if state.skills else {"count": 0, "skills": []}


@router.post("/skills/reload", dependencies=[Depends(require_admin)])
async def reload_skills():
    return state.skills.summary() if state.skills and state.skills.reload() is not None else {"count": 0}


@router.get("/monitor", dependencies=[Depends(current_principal)])
async def monitor_status():
    """运行状态：Agent/工具实时统计 + 进程内监控（SLO 燃烧率、触发中的告警、处置建议）。"""
    return {
        "agents": state.orchestrator.get_stats() if state.orchestrator else {},
        "tools": state.tool_manager.monitor_snapshot() if state.tool_manager else {},
        "agent_tools": state.orchestrator.tool_executor.snapshot() if state.orchestrator else {},
        "workflow": {"pending_actions": await asyncio.to_thread(state.workflow.pending_work)} if state.workflow else None,
        "channels": await asyncio.to_thread(state.channels.problems) if state.channels else None,
        "monitor": state.monitor.summary() if state.monitor else None,
        "mcp": state.mcp_client.summary() if state.mcp_client else {"enabled": False, "servers": []},
    }


@router.get("/metrics")
async def prometheus_metrics():  # 不能命名为 metrics：会遮蔽上方导入的 monitor.metrics 模块
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _owns_trace(trace: Optional[Dict[str, Any]], principal: Principal) -> bool:
    owner = ((trace or {}).get("observability") or {}).get("owner")
    return principal.can_read_all or (owner is not None and owner == principal.subject)


# 静态路径必须在 /trace/{request_id} 之前注册，否则会被当作 request_id 匹配。
@router.get("/trace/tools")
async def trace_tools(limit: int = 20, principal: Principal = Depends(require_roles(ROLE_IT_ADMIN, ROLE_AUDITOR))):
    return {"items": await state.memory.recent_traces(limit) if state.memory else []}


async def _readable_trace(request_id: str, principal: Principal):
    trace = await state.memory.get_trace(request_id) if state.memory else None
    if trace is not None and not _owns_trace(trace, principal):
        raise forbid("只能查看自己的请求 trace")
    return trace


@router.get("/trace/tool/{request_id}")
async def trace_tool(request_id: str, principal: Principal = Depends(current_principal)):
    trace = await _readable_trace(request_id, principal)
    return {"request_id": request_id, "found": trace is not None, "trace": trace or {}}


@router.get("/trace/{request_id}")
async def trace_request(request_id: str, principal: Principal = Depends(current_principal)):
    """Canonical replay endpoint for plan, tools, memory and approval events (owner, admin or auditor only)."""
    trace = await _readable_trace(request_id, principal)
    return {"request_id": request_id, "found": trace is not None, "trace": trace or {}, "schema_version": 1}


# 不调用模型的 v1 离线门禁（evaluate_all.py）；其余是真实模型评测。
SMOKE_SUITES = {"intent", "rag", "memory", "planner", "safety"}
# 离线评测套件 → 前端展示的关键指标（label, 报告中的取值路径, 展示格式）。
EVAL_SUITES = (
    ("application_v2", "应用留出集 · 真实模型 / 本地业务", "application_holdout_live.json", (("单 Agent 成功率", "summary.single.success_rate", "percent"), ("多 Agent 成功率", "summary.bounded.success_rate", "percent"), ("每策略运行数", "summary.single.trials", "number"))),
    ("intent_v3", "意图难例集 · 真实模型", "intent_v3_llm.json", (("准确率（含可接受）", "accuracy", "percent"), ("严格准确率", "strict_accuracy", "percent"), ("用例数", "total", "number"))),
    ("rag_v2", "检索消融 · 60 文档 / 40 查询", "rag_ablation_v2.json", (("BM25 Recall@3", "strategies.bm25.recall_at_3", "percent"), ("Vector Recall@3", "strategies.vector.recall_at_3", "percent"), ("Hybrid Recall@3", "strategies.hybrid.recall_at_3", "percent"))),
    ("intent", "意图识别", "intent_latest.json", (("准确率", "accuracy", "percent"), ("Macro-F1", "macro_f1", "percent"), ("用例数", "total", "number"))),
    ("rag", "混合检索 RAG", "rag_latest.json", (("Hybrid Recall@5", "hybrid.recall_at_5", "percent"), ("Hybrid MRR", "hybrid.mrr", "number"), ("Vector Recall@5", "vector.recall_at_5", "percent"))),
    ("memory", "三级记忆", "memory_latest.json", (("情景召回", "metrics.episodic_recall", "percent"), ("无关注入率", "metrics.unrelated_injection_rate", "percent"))),
    ("planner", "有界 Planner", "planner_latest.json", (("计划有效率", "plan_validity", "percent"), ("用例数", "total", "number"))),
    ("safety", "安全回归", "phase6_latest.json", (("通过率", "safety_pass_rate", "percent"), ("检查项", "total", "number"))),
)


def _dig(data: Any, path: str) -> Any:
    for key in path.split("."):
        data = data.get(key) if isinstance(data, dict) else None
    return data


@router.get("/eval/reports")
async def eval_reports():
    """最新离线评测报告摘要（scripts/evaluate_all.py 生成）；只读文件，不触发模型调用。"""
    reports_dir = pathlib.Path(_ROOT) / "evaluation/reports"

    def load(name: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads((reports_dir / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    suites = []
    for suite_id, title, filename, fields in EVAL_SUITES:
        report = await asyncio.to_thread(load, filename)
        suites.append({
            "id": suite_id, "title": "历史 smoke · " + title if suite_id in SMOKE_SUITES else title, "status": (report or {}).get("status", "missing"),
            "metrics": [{"label": label, "value": _dig(report, path), "format": fmt} for label, path, fmt in fields] if report else [],
        })
    gate = await asyncio.to_thread(load, "all_latest.json") or {}
    return {"gate": {"status": gate.get("status", "missing"), "all_passed": bool(gate.get("all_passed"))}, "suites": suites}
