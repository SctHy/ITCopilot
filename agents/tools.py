"""IT-Copilot Agent 工具：本地只读查询、只生成待审批请求的写工具，以及共享知识检索。

BaseAgent 把每个 spec 包成 PydanticAI Tool；PydanticAI 驱动模型/工具循环，BaseAgent 负责
JSON Schema 校验、写操作守卫与 trace 记录。
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


# ── 只读工具：读取本人的沙箱台账（workflow.store.WorkflowStore.employee） ──────

def _own_record(req, directory):
    """当前登录员工的台账；只按已验证身份查询，模型传入的工号不参与。"""
    principal = getattr(req, "principal", None)
    employee_id = getattr(principal, "employee_id", None)
    if directory is None:
        raise ValueError("台账服务不可用")
    if not employee_id:
        raise ValueError("当前登录身份未绑定工号，无法查询台账")
    return directory.employee(employee_id) or {"devices": [], "tickets": []}


def lookup_asset(req, args, directory):
    asset_id = str(args.get("asset_id", "")).strip().upper()
    devices = _own_record(req, directory)["devices"]
    device = next((d for d in devices if d["device_id"].upper() == asset_id), None)
    if device is None:
        # 不区分“不存在”和“属于他人”，避免借工具探测别人的设备。
        return {"asset_id": asset_id, "found": False, "own_devices": [d["device_id"] for d in devices],
                "message": "你名下的设备台账里没有这个资产号"}
    return {"asset_id": device["device_id"], "found": True, "kind": device["kind"], "locked": device["locked"], "source": "device_ledger"}


def query_ticket(req, args, directory):
    ticket_id = str(args.get("ticket_id", "")).strip().upper()
    tickets = _own_record(req, directory)["tickets"]
    if ticket_id:
        tickets = [t for t in tickets if t["ticket_id"].upper() == ticket_id]
    return {"tickets": tickets, "found": bool(tickets), "source": "ticket_ledger",
            "message": "" if tickets else "没有找到你名下的这张工单；仍在审批中的建单申请不在工单台账里"}


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


def lock_device(req, args):
    return _pending(req, args, "lock_device")


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
        search_knowledge_base, required=["query"], self_governed=True,
    ))


# ── Agent 工具集 ──────────────────────────────────────────────────────────────

def agent_tools(directory=lambda: None) -> ToolSet:
    """directory：返回台账读模型（WorkflowStore）的函数；运行时装配完成后才有值。"""
    access_schema = {**_strings("employee_id", "reason"),
        "system": {"type": "string", "enum": ["GitLab", "Jira", "VPN"]},
        "role": {"type": "string", "enum": ["readonly", "reporter", "developer"], "description": "不得把管理员等其他角色换成这里的角色"},
        "duration_hours": {"type": "integer", "minimum": 1, "maximum": 168, "description": "有效期（小时）；用户没说就省略"}}
    return _toolset(
        make_tool("lookup_asset", "查询本人名下设备的资产信息与锁定状态，asset_id 如 DEV-1001。", _strings("asset_id"),
                  lambda req, args: lookup_asset(req, args, directory()), required=["asset_id"]),
        make_tool("query_ticket", "查询本人名下已创建的工单；不传 ticket_id 时列出全部。", _strings("ticket_id"),
                  lambda req, args: query_ticket(req, args, directory())),
        make_tool("lock_device", "设备丢失或被盗时，创建远程锁定待审批请求；只能锁本人设备，device_id 如 DEV-1001。",
                  _strings("employee_id", "device_id", "reason"), lock_device, required=["employee_id", "device_id", "reason"], retry_safe=False),
        make_tool("reset_password", "创建密码重置待审批请求，不直接修改账号。", _strings("employee_id"), reset_password, required=["employee_id"], retry_safe=False),
        make_tool("unlock_account", "创建账号解锁待审批请求。", _strings("employee_id"), unlock_account, required=["employee_id"], retry_safe=False),
        make_tool("request_access", "创建限时权限待审批请求，到期自动回收；只能为本人申请。字段以本工具参数为准，SOP 里的其他信息写进 reason。", access_schema, request_access, required=["employee_id", "system", "role", "reason"], retry_safe=False),
        make_tool("create_ticket", "用户要求建单时，创建工单待审批请求。title 写故障摘要，description 写已做的诊断与证据，priority 取 P1-P4。",
                  _strings("title", "description", "priority"), create_ticket, required=["title", "description"], retry_safe=False),
    )
