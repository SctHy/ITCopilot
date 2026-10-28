"""IT-Copilot 企业 IT 服务台 API。"""
import asyncio
import json
import logging
import os
import pathlib
import sys
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from typing import Any, Dict, Optional

_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from core.events import TERMINAL_EVENTS, EventPublisher, InMemoryEventBus, RedisEventBus
from core.identity import (ROLE_APPROVER, ROLE_AUDITOR, ROLE_IT_ADMIN, AuthError, Principal, dev_mode_allowed, dev_users, issue_dev_token,
                           principal_from_token)
from monitor import metrics
from tooling.schemas import KNOWLEDGE_SEARCH_SCHEMA
from workflow.policy import PolicyViolation
from workflow.store import ApprovalConflict, ApprovalForbidden

load_dotenv(pathlib.Path(_ROOT) / ".env")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("it_copilot")

_orchestrator = None
_memory = None
_tool_manager = None
_kb = None
_skills = None
_profile_updater = None
_tracing = None
_monitor = None
_mcp_client = None
_workflow = None
_worker = None
_registry = None
# 未接 Redis 时（单元测试、显式降级）使用进程内事件流；lifespan 中有 Redis 则替换为 Redis Streams。
_events = InMemoryEventBus()
# 保存后台任务引用：事件循环只持有弱引用，未被引用的任务可能在执行中途被回收。
_background_tasks: set = set()

class ChatRequest(BaseModel):
    """身份（用户、部门、角色）只来自已验证的 token；请求体里的 user_id/department 等字段会被忽略。"""
    message: str = Field(..., min_length=1, max_length=8000)
    conv_id: str = Field("demo", min_length=1, max_length=200)
    client_request_id: Optional[str] = Field(None, min_length=1, max_length=200)


class ChatResponse(BaseModel):
    request_id: str
    response: str
    status: str
    agent_type: str
    intent: str
    intent_group: str = "other"
    entities: Dict[str, Any] = {}
    confidence: float = 0.
    intent_confidence: float = 0.
    routing_confidence: float = 0.
    intent_source_scores: Dict[str, float] = {}
    clarification_needed: bool = False
    missing_slots: list[str] = []
    inherited_from_previous: bool = False
    trace_id: str = ""
    tools_used: list[str] = []
    knowledge_used: bool = False
    latency_ms: float = 0.
    escalated: bool = False
    plan: Optional[Dict[str, Any]] = None
    findings: list[Dict[str, Any]] = []
    approval_required: bool = False
    approval_id: Optional[str] = None
    token_usage: Dict[str, int] = {}


class KnowledgeDocument(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=200_000)
    department: Optional[str] = Field(None, max_length=80)
    document_id: Optional[str] = Field(None, min_length=1, max_length=200)


class KnowledgeAddRequest(BaseModel):
    documents: list[KnowledgeDocument] = Field(..., min_length=1, max_length=100)


class ApprovalDecision(BaseModel):
    """审批人由 token 决定，不接受请求体中的 actor。"""
    decision: str = Field(..., pattern="^(approved|rejected)$")
    payload_hash: str = Field(..., min_length=64, max_length=64)


class DevTokenRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=80)


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_on_background_done)


def _on_background_done(task: asyncio.Task) -> None:
    _background_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.warning("background task failed: %s", type(task.exception()).__name__)


def current_principal(authorization: Optional[str] = Header(None)) -> Principal:
    scheme, _, token = (authorization or "").partition(" ")
    try:
        if scheme.lower() != "bearer":
            raise AuthError("missing bearer token")
        return principal_from_token(token.strip())
    except AuthError as exc:
        metrics.AUTH_FAILURES.labels(reason="unauthenticated").inc()
        raise HTTPException(401, str(exc), headers={"WWW-Authenticate": "Bearer"}) from None


def require_roles(*roles: str):
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has_any(*roles):
            metrics.AUTH_FAILURES.labels(reason="forbidden").inc()
            raise HTTPException(403, f"需要以下角色之一：{', '.join(roles)}")
        return principal
    return dependency


require_admin = require_roles(ROLE_IT_ADMIN)


def _forbid(message: str = "无权访问该资源"):
    metrics.AUTH_FAILURES.labels(reason="forbidden").inc()
    # 404 instead of 403 would hide existence; 403 is clearer for this internal tool.
    return HTTPException(403, message)


def _request_id(principal: Principal, client_request_id: Optional[str]) -> str:
    """客户端幂等键按调用者隔离：不同用户使用相同 client_request_id 不会共享回执、trace 或审批。"""
    if not client_request_id:
        return str(uuid.uuid4())
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"it-copilot:{principal.tenant}:{principal.subject}:{client_request_id}"))


def _chroma_settings() -> Dict[str, Any]:
    return {
        "chroma_host": os.getenv("CHROMA_HOST", "127.0.0.1"),
        "chroma_port": int(os.getenv("CHROMA_PORT", "8000")),
        "chroma_path": os.getenv("CHROMA_PERSIST_DIRECTORY", str(pathlib.Path(_ROOT) / "data/chroma")),
    }


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes"}


async def _start_mcp_client():
    """连接 config/mcp_servers.json 中启用的外部 MCP 服务；后台连接，不阻塞启动。"""
    if not _env_flag("IT_COPILOT_MCP_ENABLED"):
        return None
    from tooling.mcp_client import StandardMCPClient, load_servers
    configs = load_servers(pathlib.Path(os.getenv("IT_COPILOT_MCP_CONFIG", str(pathlib.Path(_ROOT) / "config/mcp_servers.json"))))
    client = StandardMCPClient(_tool_manager, configs, on_change=lambda: _orchestrator.set_external_tools(client.agent_tools()))
    await client.start(wait=False)
    logger.info("external MCP client started: %s", [c.name for c in configs])
    return client


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _orchestrator, _memory, _tool_manager, _kb, _skills, _profile_updater, _tracing, _monitor, _mcp_client, _workflow, _worker, _registry, _events
    from agents.agent_orchestrator import AgentOrchestrator
    from core.concurrency import ProfileUpdater
    from core.skill_loader import SkillManager
    from core.telemetry import setup_tracing, shutdown_tracing
    from memory.conversation_memory import MemoryManager
    from monitor.performance_monitor import PerformanceMonitor
    from tooling.knowledge_base import KnowledgeBase
    from tooling.mcp_server import create_http_app, create_server
    from tooling.tool_manager import MCPToolManager, Tool
    from workflow.store import WorkflowStore
    from workflow.worker import ExecutionWorker, build_registry, database_url

    _tracing = setup_tracing()
    llm = {"api_key": os.getenv("ANTHROPIC_API_KEY", "sk-local"), "base_url": os.getenv("ANTHROPIC_BASE_URL") or None, "model": os.getenv("ANTHROPIC_MODEL", "deepseek-chat")}
    _skills = SkillManager(os.getenv("IT_COPILOT_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills")))
    _skills.load()
    _tool_manager = MCPToolManager(**llm)
    _kb = KnowledgeBase(**_chroma_settings())

    async def knowledge(params, ctx=None):
        return await _kb.search_handler(params, ctx)

    _tool_manager.register(Tool(
        name="knowledge_search", description="IT 知识库混合检索", handler=knowledge, schema=KNOWLEDGE_SEARCH_SCHEMA,
        cache_ttl=60, timeout_s=8, fallback=lambda p, c, e: {"items": [], "fallback": True, "error": e},
    ))
    _orchestrator = AgentOrchestrator(**llm, skill_manager=_skills, rag_tool_manager=_tool_manager)
    try:
        _memory = MemoryManager(redis_url=os.getenv("REDIS_URL", "redis://:it-copilot123@127.0.0.1:6379/0"), **_chroma_settings(), **llm)
        # Redis 属于正确性边界：锁和幂等不可省略，仅构造惰性客户端不足以证明可用。
        await _memory.ping()
    except Exception as exc:
        _memory = None
        if os.getenv("IT_COPILOT_REQUIRE_REDIS", "true").lower() not in {"0", "false", "no"}:
            logger.error("Redis is required for safe startup: %s", type(exc).__name__)
            raise RuntimeError("Redis 不可用，拒绝启动：会话锁和幂等保护不能被绕过") from exc
        logger.warning("memory degraded by explicit IT_COPILOT_REQUIRE_REDIS=false: %s", type(exc).__name__)
    # 审批与执行状态的唯一来源是工作流数据库（SQLite 本地 / PostgreSQL 部署），不放在 Redis 里。
    _workflow = await asyncio.to_thread(WorkflowStore, database_url(_chroma_settings()["chroma_path"]))
    _registry = build_registry(_workflow)
    _workflow.route = lambda action: _registry.for_action(action).name
    _worker = ExecutionWorker(_workflow, _registry, poll_s=float(os.getenv("IT_COPILOT_WORKER_POLL_S", "1")))
    if _env_flag("IT_COPILOT_WORKER_ENABLED", "true"):
        _worker.start()
    _events = RedisEventBus(_memory._redis) if _memory else InMemoryEventBus()
    if _memory:
        # 后台维护（会话压缩 + 画像提炼）走有界 worker，同一用户的积压更新合并为一次。
        _profile_updater = ProfileUpdater(_memory, workers=int(os.getenv("IT_COPILOT_MEMORY_WORKERS", "2")))
        _profile_updater.start()

    async with AsyncExitStack() as stack:
        # 对外 MCP 服务端：/mcp 暴露本地知识库检索。
        mcp_server = create_server(_kb, _tool_manager)
        _mcp_mount.app = create_http_app(mcp_server)
        await stack.enter_async_context(mcp_server.session_manager.run())
        _mcp_client = await _start_mcp_client()
        _monitor = PerformanceMonitor(_orchestrator, _tool_manager, _mcp_client)
        await _monitor.start()
        if dev_mode_allowed():
            logger.warning("IT_COPILOT_AUTH_MODE=dev：使用本地演示身份签发 token，仅适用于开发与评测")
        logger.info("IT-Copilot ready on port %s", os.getenv("IT_COPILOT_PORT", "8001"))
        try:
            yield
        finally:
            await _monitor.stop()
            await _worker.stop()
            for adapter in _registry.all():
                if hasattr(adapter, "close"):
                    await adapter.close()
            _workflow.close()
            if _mcp_client:
                await _mcp_client.close()
            _mcp_mount.app = None
    if _background_tasks:
        await asyncio.gather(*_background_tasks, return_exceptions=True)
    if _profile_updater:
        await _profile_updater.close()
    shutdown_tracing(_tracing)
    if _memory:
        with suppress(Exception):
            await _memory.close()


class _LazyMount:
    """/mcp 挂载点：lifespan 完成前返回 503，之后转发给 MCP Streamable HTTP 应用。"""

    def __init__(self):
        self.app = None

    async def __call__(self, scope, receive, send):
        if self.app is None:
            response = JSONResponse({"detail": "MCP server not ready"}, status_code=503)
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


_mcp_mount = _LazyMount()

app = FastAPI(title="IT-Copilot", version="0.1.0", lifespan=lifespan)
_cors_origins = os.getenv("IT_COPILOT_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173")
app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in _cors_origins.split(",") if o.strip()], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
app.mount("/mcp", _mcp_mount)


@app.get("/health")
async def health():
    agents = _orchestrator.agent_types() if _orchestrator else []
    return {"status": "ok", "service": "IT-Copilot", "port": int(os.getenv("IT_COPILOT_PORT", "8001")), "agents": agents}


@app.get("/auth/config")
async def auth_config():
    """前端登录方式：dev 模式列出演示身份；oidc 模式给出 Keycloak 地址与 client。"""
    if dev_mode_allowed():
        return {"mode": "dev", "users": [{"username": u, "department": d.get("department"), "roles": d.get("roles", []),
                                          "employee_id": d.get("employee_id")} for u, d in dev_users().items()]}
    return {"mode": os.getenv("IT_COPILOT_AUTH_MODE", "dev"), "issuer": os.getenv("OIDC_PUBLIC_ISSUER") or os.getenv("OIDC_ISSUER"),
            "client_id": os.getenv("OIDC_FRONTEND_CLIENT_ID", "it-copilot-web")}


@app.post("/auth/dev-token")
async def dev_token(payload: DevTokenRequest):
    if not dev_mode_allowed():
        raise HTTPException(404, "not found")
    try:
        return {"access_token": issue_dev_token(payload.username), "token_type": "Bearer"}
    except AuthError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/auth/me")
async def auth_me(principal: Principal = Depends(current_principal)):
    return principal.public()


@app.get("/skills")
async def skills():
    return _skills.summary() if _skills else {"count": 0, "skills": []}


@app.post("/skills/reload", dependencies=[Depends(require_admin)])
async def reload_skills():
    return _skills.summary() if _skills and _skills.reload() is not None else {"count": 0}


@app.get("/monitor", dependencies=[Depends(current_principal)])
async def monitor():
    """运行状态：Agent/工具实时统计 + 进程内监控（SLO 燃烧率、触发中的告警、处置建议）。"""
    return {
        "agents": _orchestrator.get_stats() if _orchestrator else {},
        "tools": _tool_manager.monitor_snapshot() if _tool_manager else {},
        "agent_tools": _orchestrator.tool_executor.snapshot() if _orchestrator else {},
        "workflow": {"pending_actions": await asyncio.to_thread(_workflow.pending_work)} if _workflow else None,
        "monitor": _monitor.summary() if _monitor else None,
        "mcp": _mcp_client.summary() if _mcp_client else {"enabled": False, "servers": []},
    }


@app.get("/metrics")
async def prometheus_metrics():  # 不能命名为 metrics：会遮蔽上方导入的 monitor.metrics 模块
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _owns_trace(trace: Optional[Dict[str, Any]], principal: Principal) -> bool:
    owner = ((trace or {}).get("observability") or {}).get("owner")
    return principal.can_read_all or (owner is not None and owner == principal.subject)


# 静态路径必须在 /trace/{request_id} 之前注册，否则会被当作 request_id 匹配。
@app.get("/trace/tools")
async def trace_tools(limit: int = 20, principal: Principal = Depends(require_roles(ROLE_IT_ADMIN, ROLE_AUDITOR))):
    return {"items": await _memory.recent_traces(limit) if _memory else []}


async def _readable_trace(request_id: str, principal: Principal):
    trace = await _memory.get_trace(request_id) if _memory else None
    if trace is not None and not _owns_trace(trace, principal):
        raise _forbid("只能查看自己的请求 trace")
    return trace


@app.get("/trace/tool/{request_id}")
async def trace_tool(request_id: str, principal: Principal = Depends(current_principal)):
    trace = await _readable_trace(request_id, principal)
    return {"request_id": request_id, "found": trace is not None, "trace": trace or {}}


@app.get("/trace/{request_id}")
async def trace_request(request_id: str, principal: Principal = Depends(current_principal)):
    """Canonical replay endpoint for plan, tools, memory and approval events (owner, admin or auditor only)."""
    trace = await _readable_trace(request_id, principal)
    return {"request_id": request_id, "found": trace is not None, "trace": trace or {}, "schema_version": 1}


def _require_workflow():
    if not _workflow:
        raise HTTPException(503, "approval store unavailable")
    return _workflow


async def _readable_approval(approval_id: str, principal: Principal) -> Dict[str, Any]:
    store = _require_workflow()
    body = await asyncio.to_thread(store.get, approval_id)
    if not body:
        raise HTTPException(404, "approval not found")
    if not store.can_read(body, principal):
        raise _forbid("只能查看自己发起的申请")
    return body


@app.get("/approvals")
async def list_approvals(status: Optional[str] = Query(None, pattern="^(pending|approved|rejected|expired|superseded)$"),
                         principal: Principal = Depends(current_principal)):
    """申请人看到自己的申请；审批人、管理员、审计员看到全部（审批收件箱）。"""
    return {"items": await asyncio.to_thread(_require_workflow().list, principal, status)}


@app.get("/approvals/{approval_id}")
async def get_approval(approval_id: str, principal: Principal = Depends(current_principal)):
    return await _readable_approval(approval_id, principal)


@app.post("/approvals/{approval_id}")
async def decide_approval(approval_id: str, payload: ApprovalDecision, principal: Principal = Depends(require_roles(ROLE_APPROVER, ROLE_IT_ADMIN))):
    store = _require_workflow()
    before = await asyncio.to_thread(store.get, approval_id)
    try:
        body = await asyncio.to_thread(store.decide, approval_id, payload.decision, principal, payload.payload_hash)
    except ApprovalForbidden as exc:
        metrics.AUTH_FAILURES.labels(reason="separation_of_duties").inc()
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not body:
        raise HTTPException(404, "approval not found")
    if before and before["status"] == "pending" and body["status"] != "pending":
        # 只统计真正发生的状态迁移；重复提交的幂等返回不计数。
        metrics.APPROVALS.labels(event=body["status"]).inc()
    if body["status"] == "approved" and _worker:
        # 执行由持久 worker 完成；这里只短暂等待，让快速路径直接返回结果，慢的外部系统返回 queued/executing。
        body = await _worker.wait_settled(approval_id, float(os.getenv("IT_COPILOT_APPROVAL_WAIT_S", "5"))) or body
    return body


@app.post("/approvals/{approval_id}/reconcile")
async def reconcile_approval(approval_id: str, principal: Principal = Depends(require_admin)):
    """人工对账：把 manual_review/unknown 的动作重新交给 worker，先回查目标系统再决定是否重做。"""
    body = await asyncio.to_thread(_require_workflow().requeue, approval_id, principal)
    if not body:
        raise HTTPException(404, "approval not found")
    if _worker:
        body = await _worker.wait_settled(approval_id, float(os.getenv("IT_COPILOT_APPROVAL_WAIT_S", "5"))) or body
    return body


@app.get("/approvals/{approval_id}/audit")
async def approval_audit(approval_id: str, principal: Principal = Depends(current_principal)):
    await _readable_approval(approval_id, principal)
    return {"events": await asyncio.to_thread(_workflow.audit, approval_id)}


@app.get("/business/employees/{employee_id}")
async def business_employee(employee_id: str, principal: Principal = Depends(current_principal)):
    """沙箱目录中的员工状态与限时授权台账：本人、审批人、管理员、审计员可查。"""
    if employee_id != principal.employee_id and not principal.has_any(ROLE_APPROVER, ROLE_IT_ADMIN, ROLE_AUDITOR):
        raise _forbid("只能查看本人账号状态")
    body = await asyncio.to_thread(_require_workflow().employee, employee_id)
    if not body:
        raise HTTPException(404, "employee not found")
    return body


MAX_UPLOAD_BYTES = 10 * 1024 * 1024


async def _add_knowledge(documents: list[Dict[str, Any]]) -> Dict[str, Any]:
    if not _kb:
        raise HTTPException(503, "service not ready")
    try:
        chunks = await _kb.add_documents_async(documents)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # 让 Agent 立即检索到新文档，而不是等待 60 秒 TTL 缓存过期。
    _tool_manager.invalidate_cache("knowledge_search")
    return {"documents": len(documents), "chunks": chunks, "total_chunks": await _kb.doc_count_async()}


@app.post("/knowledge/add", dependencies=[Depends(require_admin)])
async def knowledge_add(payload: KnowledgeAddRequest):
    return await _add_knowledge([doc.model_dump(exclude_none=True) for doc in payload.documents])


@app.post("/knowledge/upload", dependencies=[Depends(require_admin)])
async def knowledge_upload(file: UploadFile = File(...)):
    """导入 .txt / .md（整篇为一个文档，标题取文件名）或 .json（文档数组或 {"documents": [...]})。"""
    name = pathlib.Path(file.filename or "upload.txt")
    suffix = name.suffix.lower()
    if suffix not in {".txt", ".md", ".json"}:
        raise HTTPException(415, "仅支持 .txt、.md、.json")
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "文件超过 10 MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "文件必须是 UTF-8 编码") from exc
    if suffix != ".json":
        return await _add_knowledge([{"title": name.stem, "content": text}])
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, f"JSON 解析失败：第 {exc.lineno} 行") from exc
    try:
        payload = KnowledgeAddRequest.model_validate(data if isinstance(data, dict) else {"documents": data})
    except Exception as exc:
        raise HTTPException(422, "JSON 需为文档数组，每项包含 title 与 content") from exc
    return await _add_knowledge([doc.model_dump(exclude_none=True) for doc in payload.documents])


# 离线评测套件 → 前端展示的关键指标（label, 报告中的取值路径, 展示格式）。
EVAL_SUITES = (
    ("application_v2", "应用留出集 · 真实模型 / 本地业务", "application_holdout_live.json", (("单 Agent 成功率", "summary.single.success_rate", "percent"), ("多 Agent 成功率", "summary.bounded.success_rate", "percent"), ("每策略运行数", "summary.single.trials", "number"))),
    ("intent_v2", "意图留出集 · 真实模型", "intent_ablation_live.json", (("LLM 准确率", "strategies.llm.accuracy", "percent"), ("融合准确率", "strategies.fusion.accuracy", "percent"), ("留出样本", "test_n", "number"))),
    ("rag_v2", "检索消融 · 60 文档 / 40 查询", "rag_ablation_v2.json", (("BM25 Recall@3", "strategies.bm25.recall_at_3", "percent"), ("Vector Recall@3", "strategies.vector.recall_at_3", "percent"), ("Hybrid Recall@3", "strategies.hybrid.recall_at_3", "percent"))),
    ("intent", "意图识别", "intent_latest.json", (("准确率", "accuracy", "percent"), ("Macro-F1", "macro_f1", "percent"), ("用例数", "total", "number"))),
    ("calibration", "置信度校准", "intent_calibration_latest.json", (("ECE 校准前", "ece_before", "number"), ("ECE 校准后", "ece_after", "number"), ("Brier 校准后", "brier_after", "number"))),
    ("rag", "混合检索 RAG", "rag_latest.json", (("Hybrid Recall@5", "hybrid.recall_at_5", "percent"), ("Hybrid MRR", "hybrid.mrr", "number"), ("Vector Recall@5", "vector.recall_at_5", "percent"))),
    ("memory", "三级记忆", "memory_latest.json", (("情景召回", "metrics.episodic_recall", "percent"), ("无关注入率", "metrics.unrelated_injection_rate", "percent"))),
    ("planner", "有界 Planner", "planner_latest.json", (("计划有效率", "plan_validity", "percent"), ("用例数", "total", "number"))),
    ("safety", "安全回归", "phase6_latest.json", (("通过率", "safety_pass_rate", "percent"), ("检查项", "total", "number"))),
)


def _dig(data: Any, path: str) -> Any:
    for key in path.split("."):
        data = data.get(key) if isinstance(data, dict) else None
    return data


@app.get("/eval/reports")
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
            "id": suite_id, "title": title if suite_id.endswith("_v2") else "历史 smoke · " + title, "status": (report or {}).get("status", "missing"),
            "metrics": [{"label": label, "value": _dig(report, path), "format": fmt} for label, path, fmt in fields] if report else [],
        })
    gate = await asyncio.to_thread(load, "all_latest.json") or {}
    return {"gate": {"status": gate.get("status", "missing"), "all_passed": bool(gate.get("all_passed"))}, "suites": suites}


@app.get("/knowledge/stats")
async def knowledge_stats():
    return {"documents": await _kb.doc_count_async() if _kb else 0}


@app.post("/search")
async def search(query: str = Query("", min_length=1), top_k: int = Query(5, ge=1, le=20), department: Optional[str] = Query(None),
                 mode: str = Query("hybrid", pattern="^(hybrid|vector|bm25)$"), principal: Principal = Depends(current_principal)):
    if not _tool_manager:
        raise HTTPException(503, "service not ready")
    # 检索范围由身份决定；只有管理员可以指定其他部门（运维排查用）。
    if not principal.is_admin or department is None:
        department = principal.department
    result = await _tool_manager.call("knowledge_search", {"query": query, "top_k": top_k, "department": department, "mode": mode}, {})
    return result.data if result.success else {"items": [], "fallback": True, "error": result.error}


async def _run_turn(payload: ChatRequest, principal: Principal, rid: str, conv_id: str, emit=None):
    """一次对话回合：读取记忆 → 意图识别 → 组装请求 → 编排执行。用户与部门取自已验证身份。"""
    from agents.agent_orchestrator import Request
    from core.conversation_state import known_entities, merge_state

    user_id = principal.subject
    mem_ctx = await _memory.get_context(user_id, conv_id, query=payload.message) if _memory else None
    history = [{"role": m.role.value, "content": m.content} for m in mem_ctx.recent_messages[-5:]] if mem_ctx and mem_ctx.recent_messages else None
    state = (getattr(mem_ctx, "state", {}) or {}) if mem_ctx else {}
    intent_result = await _orchestrator.recognize_intent(payload.message, history=history, previous_intent_group=state.get("intent_group"))
    if emit:
        await emit("intent", {"intent": intent_result.intent.value, "intent_group": intent_result.intent_group,
                              "confidence": round(intent_result.confidence, 4), "source": intent_result.recognition_source})

    req = Request(
        message=payload.message, user_id=user_id, conv_id=conv_id, context=mem_ctx.to_prompt_text() if mem_ctx else "",
        history=history, entities=intent_result.entities, intent=intent_result.intent, intent_group=intent_result.intent_group,
        urgency=intent_result.urgency, intent_confidence=intent_result.confidence, department=principal.department, request_id=rid,
        principal=principal, emit=emit,
    )
    req.context += f"\n[当前登录员工]\n工号 {principal.employee_id or '未绑定'}；部门 {principal.department}"
    # 缺失字段按跨轮累积的实体计算：上一轮已给出的工号等不应再次阻断写操作。
    req.conversation_state = merge_state(state, intent_result.intent_group, intent_result.entities, payload.message, intent_result.missing_slots, intent=intent_result.intent.value)
    req.entities = known_entities(req.conversation_state, intent_result.entities)

    async def before_unsafe_tool(name):
        # Chat tools only prepare immutable approval data. Actual effects happen in the
        # execution worker after approval, so a crashed dry-run is retryable.
        if not _workflow:
            raise ValueError("审批存储不可用，无法创建写操作申请")

    req.before_unsafe_tool = before_unsafe_tool
    return await _orchestrator.run(req), req, intent_result, mem_ctx


def _build_trace(result, intent_result, mem_ctx, approval, approval_id, principal: Principal) -> Dict[str, Any]:
    trace = json.loads(json.dumps(result.__dict__, default=lambda value: value.value if hasattr(value, "value") else str(value)))
    trace["observability"] = {
        "schema_version": 3,
        "owner": principal.subject,
        "department": principal.department,
        "memory": {
            "working_hit": bool(mem_ctx and mem_ctx.recent_messages),
            "episodic_hits": len(mem_ctx.relevant_history) if mem_ctx else 0,
            "profile_hit": bool(mem_ctx and mem_ctx.user_profile),
        },
        "intent": {"source": intent_result.recognition_source, "confidence": intent_result.confidence, "group_agreement": intent_result.source_scores.get("group_agreement")},
        "approval": {"required": bool(approval), "approval_id": approval_id},
        "fallback": {"intent_source": intent_result.recognition_source},
    }
    return trace


@app.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest, principal: Principal = Depends(current_principal)):
    return await _chat_entry(payload, principal, _request_id(principal, payload.client_request_id))


async def _chat_entry(payload: ChatRequest, principal: Principal, rid: str, emit=None):
    if not _orchestrator:
        raise HTTPException(503, "service not ready")
    from core.telemetry import chat_span
    with chat_span(rid) as span:
        response = await _chat(payload, principal, rid, emit)
        if isinstance(response, ChatResponse):
            for key in ("status", "agent_type", "intent", "intent_group"):
                span.set_attribute(f"it_copilot.{key}", getattr(response, key))
        return response


async def _chat(payload: ChatRequest, principal: Principal, rid: str, emit=None):
    request_started = time.monotonic()
    conv_id = payload.conv_id
    if _memory:
        async with _memory.conversation(principal.subject, conv_id):
            receipt = await _memory.get_turn_receipt(principal.subject, conv_id, rid)
            if receipt:
                if receipt.get("message") != payload.message:
                    raise HTTPException(409, "同一 client_request_id 不能用于不同消息")
                return receipt.get("response")
            return await _complete_turn(payload, principal, rid, conv_id, request_started, emit)
    return await _complete_turn(payload, principal, rid, conv_id, request_started, emit)


async def _complete_turn(payload, principal: Principal, rid, conv_id, request_started, emit=None):
    # The caller holds the conversation lease through approval creation AND commit.
    result, req, intent_result, mem_ctx = await _run_turn(payload, principal, rid, conv_id, emit)

    from core.approval import approval_from_traces
    approval = approval_from_traces(result.tool_traces, payload.message)
    approval_id = rid + ":approval" if approval else None
    reply = result.response
    if approval and _workflow:
        summary = {k: approval[k] for k in ("actions", "risk", "summary", "source_message")}
        try:
            created = await asyncio.to_thread(_workflow.create, approval_id, request_id=rid, conv_id=conv_id, principal=principal,
                                              action_requests=approval["action_requests"], summary=summary)
        except (PolicyViolation, ApprovalConflict, ValueError) as exc:
            # Tools validate first, so this is rare (e.g. policy changed mid-turn); never create a half-valid approval.
            logger.warning("approval not created for %s: %s", rid, exc)
            approval, approval_id = None, None
            reply += f"\n\n（审批申请未创建：{exc}）"
        else:
            metrics.APPROVALS.labels(event="created").inc()
            if created.get("superseded"):
                metrics.APPROVALS.labels(event="superseded").inc(len(created["superseded"]))
                reply += f"\n\n（本次申请已替代本会话中尚未审批的申请：{', '.join(created['superseded'])}）"
            if emit:
                await emit("approval_required", {"approval_id": approval_id, "approvals_required": created["approvals_required"],
                                                 "policy_version": created["policy_version"], "actions": approval["actions"]})

    response = ChatResponse(
        request_id=rid, response=reply, status=result.status, agent_type=result.agent_type.value,
        intent=result.intent.value if result.intent else "other", intent_group=req.intent_group or "other", entities=req.entities,
        confidence=req.intent_confidence, intent_confidence=req.intent_confidence, routing_confidence=result.routing_confidence,
        intent_source_scores=intent_result.source_scores, clarification_needed=bool(req.conversation_state.get("missing_fields")),
        missing_slots=req.conversation_state.get("missing_fields", []), inherited_from_previous=intent_result.inherited_from_previous, trace_id=result.trace_id,
        tools_used=result.tools_used, knowledge_used="search_knowledge_base" in result.tools_used, latency_ms=result.latency_ms,
        escalated=result.escalated, plan=result.plan, findings=result.findings, approval_required=bool(approval), approval_id=approval_id,
        token_usage=result.token_usage,
    )
    if _memory:
        trace = _build_trace(result, intent_result, mem_ctx, approval, approval_id, principal)
        await _memory.commit_turn(principal.subject, conv_id, rid, payload.message, response.model_dump(), trace, state=req.conversation_state)
        if _profile_updater:
            _profile_updater.submit(principal.subject, conv_id)
        if result.intent and result.intent.value not in {"greeting", "chitchat"}:
            metadata = {"intent": result.intent.value, "intent_group": req.intent_group or "other", "agent_type": result.agent_type.value, "entities": intent_result.entities}
            _spawn(_memory.store_episodic_turn(principal.subject, conv_id, rid, payload.message, response.response, metadata))
    metrics.CHAT_REQUESTS.labels(status=response.status, intent_group=response.intent_group).inc()
    metrics.CHAT_LATENCY.labels(status=response.status).observe(time.monotonic() - request_started)
    if _monitor:
        _monitor.record_chat(response.status, time.monotonic() - request_started)
    return response


def _sse(event: str, data: Any, event_id: Optional[str] = None) -> str:
    head = f"id: {event_id}\n" if event_id else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def _run_streamed(payload: ChatRequest, principal: Principal, rid: str, emit: EventPublisher) -> None:
    """后台执行一整轮并实时写入事件流。与 SSE 连接解耦：客户端断开不会取消执行，重连后续读即可。"""
    try:
        raw = await _chat_entry(payload, principal, rid, emit)
        response = raw if isinstance(raw, ChatResponse) else ChatResponse.model_validate(raw)
        # 正文是最终回答的分片（不是模型 token 流）；执行过程已经通过上面的事件实时推送。
        for index in range(0, len(response.response), 200):
            await emit("content_delta", {"delta": response.response[index:index + 200]})
        await emit("completed", response.model_dump())
    except HTTPException as exc:
        await emit("error", {"status_code": exc.status_code, "detail": exc.detail})
    except Exception:
        logger.exception("streamed chat failed")
        await emit("error", {"status_code": 500, "detail": "stream_failed"})
    finally:
        if emit.first_event_s is not None:
            metrics.STREAM_FIRST_EVENT.observe(emit.first_event_s)


async def _subscribe(stream: str, after: Optional[str]):
    heartbeat_s = float(os.getenv("IT_COPILOT_SSE_HEARTBEAT_S", "15"))
    while True:
        events = await _events.read(stream, after, block_s=heartbeat_s)
        if not events:
            yield ": heartbeat\n\n"
            continue
        for event_id, event, data in events:
            after = event_id
            yield _sse(event, data, event_id)
            if event in TERMINAL_EVENTS:
                return


def _stream_response(stream: str, after: Optional[str]) -> StreamingResponse:
    return StreamingResponse(_subscribe(stream, after), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/chat/stream")
async def chat_stream(payload: ChatRequest, principal: Principal = Depends(current_principal)):
    """实时执行事件流：intent → routed/plan → tool_started/tool_finished → approval_required → content_delta → completed。

    每个事件带 id，可用 GET /chat/stream/{request_id} + Last-Event-ID 断线续传。相同 client_request_id 的
    重复提交只订阅已有执行，不会再跑一遍。"""
    if not _orchestrator:
        raise HTTPException(503, "service not ready")
    rid = _request_id(principal, payload.client_request_id)
    stream = "chat:" + rid
    if await _events.claim(stream, principal.subject):
        emit = EventPublisher(_events, stream, time.monotonic())
        await emit("status", {"stage": "started", "request_id": rid})
        _spawn(_run_streamed(payload, principal, rid, emit))
    elif await _events.owner(stream) != principal.subject:
        raise _forbid()
    return _stream_response(stream, None)


@app.get("/chat/stream/{request_id}")
async def chat_stream_resume(request_id: str, principal: Principal = Depends(current_principal),
                             last_event_id: Optional[str] = Header(None, alias="Last-Event-ID")):
    stream = "chat:" + request_id
    owner = await _events.owner(stream)
    if owner is None:
        raise HTTPException(404, "stream not found or expired")
    if owner != principal.subject:
        raise _forbid()
    return _stream_response(stream, last_event_id)
