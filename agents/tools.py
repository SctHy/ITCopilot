"""IT-Copilot Agent tools.

Agent tool definitions deliberately use the same Request/args contract as the
original runtime.  BaseAgent wraps each spec as a PydanticAI Tool; PydanticAI
drives the model/tool loop while BaseAgent keeps whitelist, JSON Schema
validation, write-operation guards and trace recording.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

from monitor import metrics

if TYPE_CHECKING:
    from agents.agent_orchestrator import Request

AgentToolHandler = Callable[["Request", Dict[str, Any]], Union[Any, Awaitable[Any]]]
ToolSet = Dict[str, "AgentToolSpec"]


@dataclass(frozen=True)
class AgentToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: AgentToolHandler
    allowed_agents: Optional[Tuple[str, ...]] = None
    retry_safe: bool = True


def make_tool(name: str, description: str, properties: Dict[str, Any], handler: AgentToolHandler, required: Optional[List[str]] = None, *, allowed_agents: Optional[Tuple[str, ...]] = None, retry_safe: bool = True) -> AgentToolSpec:
    schema = {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}
    return AgentToolSpec(name, description, schema, handler, allowed_agents, retry_safe)


def _toolset(*specs: AgentToolSpec) -> ToolSet:
    return {spec.name: spec for spec in specs}


def _strings(*names: str) -> Dict[str, Any]:
    """若干字符串参数的 JSON Schema 属性；每次新建，避免多个 schema 共享可变对象。"""
    return {name: {"type": "string"} for name in names}


# ── 只读工具 ──────────────────────────────────────────────────────────────────

REQUIRED_FIELDS_BY_INTENT = {
    "password_reset": ["员工号或企业账号"],
    "permission_request": ["系统名称", "角色", "业务理由", "有效期", "审批人"],
    "vpn_issue": ["错误码", "设备和客户端版本"],
    "device_failure": ["设备号", "错误现象"],
    "ticket_create": ["问题标题", "影响范围"],
}

ERROR_CODES = {
    "401": ("认证失败", ["确认身份平台状态", "检查客户端时间和版本"]),
    "403": ("权限不足", ["核对最小权限申请和审批状态"]),
    "500": ("服务端处理异常", ["记录 request_id 和发生时间", "确认服务状态"]),
    "VPN-691": ("VPN 身份认证或连接失败", ["确认账号未锁定", "检查系统时间、DNS 和客户端版本"]),
}


def _enum_value(value, default=None):
    return value.value if value else default


def inspect_request_context(req, args):
    return {
        "intent": _enum_value(req.intent), "intent_group": req.intent_group, "urgency": _enum_value(req.urgency),
        "intent_confidence": round(req.intent_confidence, 4), "entities": req.entities or {}, "context_available": bool(req.context),
        "requested_focus": str(args.get("focus", "general"))[:40],
    }


def suggest_required_fields(req, args):
    intent = _enum_value(req.intent, "other")
    return {"intent": intent, "required_fields": REQUIRED_FIELDS_BY_INTENT.get(intent, ["希望解决的具体 IT 问题"]), "known_entities": req.entities or {}}


def lookup_error_code(req, args):
    code = str(args.get("error_code", "")).upper().strip()
    meaning, steps = ERROR_CODES.get(code, ("暂未识别的错误码", ["补充完整错误信息、发生时间和运行环境"]))
    return {"error_code": code, "meaning": meaning, "next_steps": steps, "server_log_checked": False}


def build_diagnostic_plan(req, args):
    return {
        "environment": str(args.get("environment", "unknown"))[:80], "reproduced": bool(args.get("reproduced", False)),
        "diagnostic_steps": ["记录完整错误信息和时间", "确认网络、DNS、代理和证书", "确认版本、配置和权限"],
    }


def lookup_asset(req, args):
    return {"asset_id": args.get("asset_id", ""), "status": "assigned", "source": "mock_asset_adapter", "read_only": True}


def check_service_status(req, args):
    return {"service": args.get("service", "corporate-vpn"), "status": "operational", "source": "mock_status_adapter", "read_only": True}


def query_ticket(req, args):
    return {"ticket_id": args.get("ticket_id", "INC-DEMO-1001"), "status": "In Progress", "owner": "IT Service Desk", "source": "mock_ticket_adapter"}


# ── 写操作工具：只生成待审批请求，不直接变更 ─────────────────────────────────

def _pending(req, args, action):
    return {"status": "pending_approval", "action": action, "request": args, "requires_approval": True, "message": "已生成待审批请求；未执行账号、权限或设备变更。"}


def reset_password(req, args):
    return _pending(req, args, "reset_password")


def request_access(req, args):
    return _pending(req, args, "request_access")


def unlock_account(req, args):
    return _pending(req, args, "unlock_account")


def create_ticket(req, args):
    return _pending(req, args, "create_ticket")


# ── 共享 RAG 工具 ─────────────────────────────────────────────────────────────

def build_shared_rag_tools(tool_manager) -> ToolSet:
    async def search_knowledge_base(req, args):
        query = str(args.get("query") or req.message or "").strip()
        top_k = args.get("top_k", 5)
        if not query:
            return {"success": False, "error": "query 不能为空", "results": []}
        if tool_manager is None:
            return {"success": False, "error": "RAG 工具未初始化", "results": []}
        result = await tool_manager.search_with_rewrite("knowledge_search", query, top_k=top_k, department=getattr(req, "department", None))
        metrics.KNOWLEDGE_SEARCHES.labels(outcome="hit" if result.success else result.outcome).inc()
        return {
            "success": bool(result.success), "query": query, "top_k": top_k, "results": result.data if result.success else [],
            "error": result.error if not result.success else "", "reranked": bool(result.reranked),
        }

    return _toolset(make_tool(
        "search_knowledge_base", "检索企业 IT 知识库并返回相关 SOP 片段。",
        {"query": {"type": "string", "minLength": 1, "maxLength": 2000}, "top_k": {"type": "integer", "minimum": 1, "maximum": 20}},
        search_knowledge_base, required=["query"],
    ))


# ── 各 Agent 的工具白名单 ─────────────────────────────────────────────────────

def _diagnostic_plan_tool(description: str) -> AgentToolSpec:
    properties = {"environment": {"type": "string"}, "reproduced": {"type": "boolean"}}
    return make_tool("build_diagnostic_plan", description, properties, build_diagnostic_plan, required=["environment", "reproduced"])


def triage_tools() -> ToolSet:
    return _toolset(
        make_tool("inspect_request_context", "查看当前 IT 请求的意图、紧急度、实体和上下文。", _strings("focus"), inspect_request_context),
        make_tool("suggest_required_fields", "建议下一轮只需补充的 IT 字段。", {}, suggest_required_fields),
    )


def endpoint_tools() -> ToolSet:
    return _toolset(
        make_tool("lookup_asset", "只读查询设备资产状态。", _strings("asset_id"), lookup_asset),
        _diagnostic_plan_tool("生成低风险设备排障顺序。"),
    )


def access_tools() -> ToolSet:
    return _toolset(
        make_tool("reset_password", "创建密码重置待审批请求，不直接修改账号。", _strings("employee_id"), reset_password, required=["employee_id"], retry_safe=False),
        make_tool("unlock_account", "创建账号解锁待审批请求。", _strings("employee_id"), unlock_account, required=["employee_id"], retry_safe=False),
        make_tool("request_access", "创建最小权限待审批请求。", _strings("employee_id", "system", "role", "reason"), request_access, required=["employee_id", "system", "role", "reason"], retry_safe=False),
    )


def network_software_tools() -> ToolSet:
    return _toolset(
        make_tool("lookup_error_code", "解释常见 IT 错误码，不读取服务端日志。", _strings("error_code"), lookup_error_code, required=["error_code"]),
        _diagnostic_plan_tool("生成低风险网络/软件排障顺序。"),
        make_tool("check_service_status", "查询 IT 服务状态。", _strings("service"), check_service_status),
    )


def ticket_tools() -> ToolSet:
    return _toolset(
        make_tool("create_ticket", "创建 IT 工单请求；第一阶段使用 mock adapter。", _strings("title", "description", "priority"), create_ticket, required=["title", "description"], retry_safe=False),
        make_tool("query_ticket", "查询工单状态。", _strings("ticket_id"), query_ticket),
    )

