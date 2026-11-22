"""FastAPI application factory and per-application resource lifecycle."""
import asyncio
import logging
import os
import pathlib
from contextlib import AsyncExitStack, asynccontextmanager

_ROOT = str(pathlib.Path(__file__).resolve().parents[3])

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from it_copilot.api.container import AppContainer
from it_copilot.api.deps import env_flag
from it_copilot.api.routes import agents, memory, approvals, auth, chat, conversations, knowledge, ops
from it_copilot.infra.env import env_float
from it_copilot.infra.events import InMemoryEventBus, RedisEventBus
from it_copilot.infra.retention import RetentionPolicy, RetentionSweeper
from it_copilot.agent.request_recall import RequestRecall
from it_copilot.infra.session_revocation import SessionRevocations
from it_copilot.infra import tenant_context
from it_copilot.infra.tenancy import TenantRegistry
from it_copilot.infra.rate_limit import MemoryRateLimiter, RedisRateLimiter, rules_from_env
from it_copilot.infra.protocols import APPLICATION
from it_copilot.infra.identity import dev_mode_allowed
from it_copilot.agent.tooling.knowledge_base import KNOWLEDGE_SEARCH_SCHEMA

load_dotenv(pathlib.Path(_ROOT) / ".env")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("it_copilot")


async def _start_mcp_client(state: AppContainer):
    """连接 config/mcp_servers.json 中启用的外部 MCP 服务；后台连接，不阻塞启动。"""
    if not env_flag("IT_COPILOT_MCP_ENABLED"):
        return None
    from it_copilot.agent.tooling.mcp_client import StandardMCPClient, load_servers
    configs = load_servers(pathlib.Path(os.getenv("IT_COPILOT_MCP_CONFIG", str(pathlib.Path(_ROOT) / "config/mcp_servers.json"))))
    client = StandardMCPClient(state.tool_manager, configs, on_change=lambda: state.orchestrator.set_external_tools(client.agent_tools()))
    await client.start(wait=False)
    logger.info("external MCP client started: %s", [c.name for c in configs])
    return client



@asynccontextmanager
async def lifespan(app: FastAPI):
    state = app.state.container
    _mcp_mount = app.state.mcp_mount
    from it_copilot.agent.agent_orchestrator import AgentOrchestrator
    from it_copilot.agent.skill_loader import SkillManager
    from it_copilot.agent.memory.conversation_memory import MemoryManager
    from it_copilot.infra.monitor.performance_monitor import PerformanceMonitor
    from it_copilot.infra.monitor.telemetry import setup_tracing, shutdown_tracing
    from it_copilot.agent.tooling.knowledge_base import KnowledgeBase
    from it_copilot.agent.tooling.mcp_server import create_http_app, create_server
    from it_copilot.agent.tooling.tool_manager import MCPToolManager, Tool
    from it_copilot.workflow.worker import ExecutionWorker, configured_store, database_url

    from it_copilot.infra.turn_records import build_sha
    sha = build_sha()
    state.lifecycle = APPLICATION.start()
    # Startup and every background loop started here (workers, pumps, sweeps) span tenants: system
    # scope. Requests do not inherit it; each enters its caller's tenant (it_copilot.api.deps, ADR 0023).
    async with AsyncExitStack() as resources:
        resources.enter_context(tenant_context.system_scope())
        resources.callback(state.lifecycle.send, "close")
        services = AsyncExitStack()
        try:
            state.tracing = setup_tracing()
            resources.callback(shutdown_tracing, state.tracing)
            llm = {"api_key": os.getenv("OPENAI_API_KEY", "sk-local"), "base_url": os.getenv("OPENAI_BASE_URL") or None, "model": os.getenv("OPENAI_MODEL", "deepseek-flash")}
            db_url = database_url(os.getenv("IT_COPILOT_DATA_DIR", str(pathlib.Path(_ROOT) / "data")))
            state.workflow, state.registry = await asyncio.to_thread(configured_store, db_url)
            resources.callback(state.workflow.close)
            for adapter in state.registry.all():
                if hasattr(adapter, "close"):
                    resources.push_async_callback(adapter.close)
            from it_copilot.infra.change_log import record_deploy
            await asyncio.to_thread(record_deploy, state.workflow.engine, sha)
            from it_copilot.agent.registry import AgentRegistry, default_spec
            state.skills = SkillManager(str(pathlib.Path(_ROOT) / default_spec().skills.root), change_engine=state.workflow.engine)
            state.skills.load()
            from it_copilot.agent.model_router import ModelRouter
            model_router = ModelRouter(engine=state.workflow.engine)
            state.tool_manager = MCPToolManager(**llm, router=model_router)
            # 审批、执行与向量（知识库、记忆）同库：本地 SQLite，部署 PostgreSQL + pgvector。
            state.kb = KnowledgeBase(db_url)

            async def knowledge(params, ctx=None):
                return await state.kb.search_handler(params, ctx)

            state.tool_manager.register(Tool(
                name="knowledge_search", description="IT 知识库混合检索", handler=knowledge, schema=KNOWLEDGE_SEARCH_SCHEMA,
                cache_ttl=60, timeout_s=8, fallback=lambda p, c, e: {"items": [], "fallback": True, "error": e},
            ))
            state.orchestrator = AgentOrchestrator(**llm, skill_manager=state.skills, rag_tool_manager=state.tool_manager, router=model_router,
                directory=state.workflow, knowledge_version=state.kb.collection_version, registry_factory=AgentRegistry)
            state.agent_registry = state.orchestrator.registry
            resources.push_async_callback(state.orchestrator.aclose)
            state.orchestrator.start()
            try:
                state.memory = MemoryManager(redis_url=os.getenv("REDIS_URL", "redis://:it-copilot123@127.0.0.1:6379/0"), database_url=db_url, **llm, router=model_router)
                resources.push_async_callback(state.memory.close)
                # Redis 属于正确性边界：锁和幂等不可省略，仅构造惰性客户端不足以证明可用。
                await state.memory.ping()
            except Exception as exc:
                state.memory = None
                if os.getenv("IT_COPILOT_REQUIRE_REDIS", "true").lower() not in {"0", "false", "no"}:
                    logger.error("Redis is required for safe startup: %s", type(exc).__name__)
                    raise RuntimeError("Redis 不可用，拒绝启动：会话锁和幂等保护不能被绕过") from exc
                logger.warning("memory degraded by explicit IT_COPILOT_REQUIRE_REDIS=false: %s", type(exc).__name__)
            state.worker = ExecutionWorker(state.workflow, state.registry, poll_s=env_float("IT_COPILOT_WORKER_POLL_S", 1))
            services.push_async_callback(state.worker.stop)
            if env_flag("IT_COPILOT_WORKER_ENABLED", "true"):
                state.worker.start()
            state.events = RedisEventBus(state.memory._redis) if state.memory else InMemoryEventBus()
            rules = rules_from_env(os.getenv)
            # Always a limiter, even with the per-identity limit off: tenants have their own (ADR 0035).
            state.rate_limiter = RedisRateLimiter(state.memory._redis, rules) if state.memory else MemoryRateLimiter(rules)
            state.request_recall = RequestRecall(state.workflow.requester_items)
            state.sessions = SessionRevocations(state.workflow.engine, cache_ttl_s=env_float("IT_COPILOT_REVOCATION_CACHE_S", 5))
            state.tenants = TenantRegistry(state.workflow.engine, cache_ttl_s=env_float("IT_COPILOT_TENANT_CACHE_S", 5))
            state.retention = RetentionSweeper(state.workflow.engine, RetentionPolicy.from_env(),
                                               interval_s=env_float("IT_COPILOT_RETENTION_INTERVAL_S", 3600))
            services.push_async_callback(state.retention.stop)
            if env_flag("IT_COPILOT_RETENTION_ENABLED", "true"):
                state.retention.start()
            mcp_server = create_server(state.kb, state.tool_manager, tenant=os.getenv("IT_COPILOT_PLATFORM_TENANT", "default"))
            _mcp_mount.app = create_http_app(mcp_server)
            resources.callback(setattr, _mcp_mount, "app", None)
            await resources.enter_async_context(mcp_server.session_manager.run())
            state.mcp_client = await _start_mcp_client(state)
            if state.mcp_client:
                resources.push_async_callback(state.mcp_client.close)
            state.monitor = PerformanceMonitor(state.orchestrator, state.tool_manager, state.mcp_client, workflow=state.workflow)
            services.push_async_callback(state.monitor.stop)
            await state.monitor.start()
            state.lifecycle.send("ready" if state.memory else "degrade")
            if dev_mode_allowed():
                logger.warning("IT_COPILOT_AUTH_MODE=dev：使用本地演示身份签发 token，仅适用于开发与评测")
            logger.info("IT-Copilot ready on port %s", os.getenv("IT_COPILOT_PORT", "8001"))
            yield
        finally:
            state.lifecycle.send("stop")
            try:
                await services.aclose()
            finally:
                # Producers stop first; requests drain before any dependency is closed.
                if state.background_tasks:
                    await asyncio.gather(*state.background_tasks, return_exceptions=True)


class _LazyMount:
    """/mcp 挂载点：lifespan 完成前返回 503，之后转发给 MCP Streamable HTTP 应用。"""

    def __init__(self):
        self.app = None

    async def __call__(self, scope, receive, send):
        if self.app is None:
            response = JSONResponse({"detail": "MCP server not ready"}, status_code=503)
            return await response(scope, receive, send)
        await self.app(scope, receive, send)


def create_app(container: AppContainer | None = None) -> FastAPI:
    _mcp_mount = _LazyMount()

    app = FastAPI(title="IT-Copilot", version="0.1.0", lifespan=lifespan)
    app.state.container = container if container is not None else AppContainer()
    app.state.mcp_mount = _mcp_mount
    _cors_origins = os.getenv("IT_COPILOT_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173")
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in _cors_origins.split(",") if o.strip()], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.mount("/mcp", _mcp_mount)
    for module in (agents, memory, ops, auth, approvals, knowledge, chat, conversations):
        app.include_router(module.router)
    return app
