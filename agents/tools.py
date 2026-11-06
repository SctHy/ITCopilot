"""IT-Copilot Agent tools.

Agent tool definitions deliberately use the same Request/args contract as the
original runtime.  BaseAgent wraps each spec as a PydanticAI Tool; PydanticAI
drives the model/tool loop while BaseAgent keeps whitelist, JSON Schema
validation, write-operation guards and trace recording.
"""
from __future__ import annotations

from typing import Any, Dict

from monitor import metrics
from tooling.tool_spec import AgentToolSpec, ToolSet, make_tool


def _toolset(*specs: AgentToolSpec) -> ToolSet:
    return {spec.name: spec for spec in specs}


def _strings(*names: str) -> Dict[str, Any]:
    """若干字符串参数的 JSON Schema 属性；每次新建，避免多个 schema 共享可变对象。"""
    return {name: {"type": "string"} for name in names}


# ── 只读工具 ──────────────────────────────────────────────────────────────────

ERROR_CODES = {
    "401": ("认证失败", ["确认身份平台状态", "检查客户端时间和版本"]),
    "403": ("权限不足", ["核对最小权限申请和审批状态"]),
    "500": ("服务端处理异常", ["记录 request_id 和发生时间", "确认服务状态"]),
    "VPN-691": ("VPN 身份认证或连接失败", ["确认账号未锁定", "检查系统时间、DNS 和客户端版本"]),
}


def lookup_error_code(req, args):
    code = str(args.get("error_code", "")).upper().strip()
    meaning, steps = ERROR_CODES.get(code, ("暂未识别的错误码", ["补充完整错误信息、发生时间和运行环境"]))
    return {"error_code": code, "meaning": meaning, "next_steps": steps, "server_log_checked": False}


def lookup_asset(req, args):
    return {"asset_id": args.get("asset_id", ""), "status": "assigned", "source": "mock_asset_adapter", "read_only": True}


def check_service_status(req, args):
    return {"service": args.get("service", "corporate-vpn"), "status": "operational", "source": "mock_status_adapter", "read_only": True}


def query_ticket(req, args):
    return {"ticket_id": args.get("ticket_id", "INC-DEMO-1001"), "status": "In Progress", "owner": "IT Service Desk", "source": "mock_ticket_adapter"}


# ── 写操作工具：只生成待审批请求，不直接变更 ─────────────────────────────────

def _pending(req, args, action):
    """Validate against the versioned access policy and the caller's identity, then
    return a dry-run. The approval store re-validates when it freezes the request."""
    from workflow.policy import load_policy
    principal = getattr(req, "principal", None)
    if action == "create_ticket" and not args.get("employee_id"):
        args["employee_id"] = (principal.employee_id if principal else None) or next(iter(req.entities.get("employee_id", [])), "")
    policy = load_policy()
    normalised = policy.normalise({"tool_name": action, "arguments": args})
    if principal is not None:
        policy.check_requester(normalised, principal.employee_id, principal.roles)
    return {"status": "pending_approval", "action": action, "request": normalised["arguments"], "requires_approval": True,
            "approvals_required": policy.requirement(normalised).approvals, "policy_version": policy.version,
            "message": "已生成待审批请求；未执行账号、权限或设备变更。"}


def reset_password(req, args):
    return _pending(req, args, "reset_password")


def request_access(req, args):
    return _pending(req, args, "request_access")


def unlock_account(req, args):
    return _pending(req, args, "unlock_account")


def create_ticket(req, args):
    return _pending(req, args, "create_ticket")


# ── 共享 RAG 工具 ─────────────────────────────────────────────────────────────

def slim_hit(item: Any) -> Any:
    """只给模型看回答和引用需要的字段：分数与三个 64 位哈希约占结果的 3/4，且模型会把长哈希抄进回答。"""
    if not isinstance(item, dict) or "content" not in item:
        return item
    doc = str(item.get("document_id") or item.get("chunk_id") or "")[:8]
    title = item.get("title", "")
    return {"title": title, "content": item["content"], "updated_at": str(item.get("updated_at", ""))[:10],
            "citation": f"{title} · {doc}:{item.get('chunk', 0)}" if doc else item.get("citation", title)}


def build_shared_rag_tools(tool_manager) -> ToolSet:
    async def search_knowledge_base(req, args):
        # 模型偶尔传空白检索词（Schema 的 minLength 拦不住空格），退回用户原话。
        query = str(args.get("query") or "").strip() or str(req.message or "").strip()
        top_k = args.get("top_k", 5)
        if not query:
            return {"success": False, "error": "query 不能为空", "results": []}
        if tool_manager is None:
            return {"success": False, "error": "RAG 工具未初始化", "results": []}
        result = await tool_manager.search("knowledge_search", query, top_k=top_k, department=getattr(req, "department", None))
        metrics.KNOWLEDGE_SEARCHES.labels(outcome="hit" if result.success else result.outcome).inc()
        return {
            "success": bool(result.success), "query": query, "results": [slim_hit(x) for x in result.data] if result.success else [],
            "error": result.error if not result.success else "",
        }

    return _toolset(make_tool(
        "search_knowledge_base", "检索企业 IT 知识库并返回相关 SOP 片段。",
        {"query": {"type": "string", "minLength": 1, "maxLength": 2000}, "top_k": {"type": "integer", "minimum": 1, "maximum": 10}},
        search_knowledge_base, required=["query"], self_governed=True, untrusted_output=True,
    ))


# ── 各 Agent 的工具白名单 ─────────────────────────────────────────────────────

def _create_ticket_tool() -> AgentToolSpec:
    # Only prepares a pending approval (no side effect), so diagnostic agents may file the
    # ticket for what they just diagnosed instead of telling the user to go elsewhere.
    return make_tool("create_ticket", "用户要求建单时，创建工单待审批请求。title 写故障摘要，description 写已做的诊断与证据，priority 取 P1-P4。",
                     _strings("title", "description", "priority"), create_ticket, required=["title", "description"], retry_safe=False)


def triage_tools() -> ToolSet:
    # 意图、实体与待补充字段已写进本轮消息，接待 Agent 不需要再用工具去查。
    return {}


def endpoint_tools() -> ToolSet:
    return _toolset(
        make_tool("lookup_asset", "只读查询设备资产状态。", _strings("asset_id"), lookup_asset),
        _create_ticket_tool(),
    )


def access_tools() -> ToolSet:
    access_schema = {**_strings("employee_id", "reason"),
        "system": {"type": "string", "enum": ["GitLab", "Jira", "VPN"]},
        "role": {"type": "string", "enum": ["readonly", "reporter", "developer"], "description": "不得把管理员等其他角色换成这里的角色"},
        "duration_hours": {"type": "integer", "minimum": 1, "maximum": 168, "description": "有效期（小时）；用户没说就省略"}}
    return _toolset(
        make_tool("reset_password", "创建密码重置待审批请求，不直接修改账号。", _strings("employee_id"), reset_password, required=["employee_id"], retry_safe=False),
        make_tool("unlock_account", "创建账号解锁待审批请求。", _strings("employee_id"), unlock_account, required=["employee_id"], retry_safe=False),
        make_tool("request_access", "创建限时权限待审批请求，到期自动回收；只能为本人申请。字段以本工具参数为准，SOP 里的其他信息写进 reason。", access_schema, request_access, required=["employee_id", "system", "role", "reason"], retry_safe=False),
        _create_ticket_tool(),
    )


def network_software_tools() -> ToolSet:
    return _toolset(
        make_tool("lookup_error_code", "解释常见 IT 错误码，不读取服务端日志。", _strings("error_code"), lookup_error_code, required=["error_code"]),
        make_tool("check_service_status", "查询 IT 服务状态。", _strings("service"), check_service_status),
        _create_ticket_tool(),
    )


def ticket_tools() -> ToolSet:
    return _toolset(
        _create_ticket_tool(),
        make_tool("query_ticket", "查询工单状态。", _strings("ticket_id"), query_ticket),
    )

