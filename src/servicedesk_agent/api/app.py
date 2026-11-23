"""FastAPI application factory and per-application resource lifecycle."""
import asyncio
import logging
import os
import pathlib
import time
from contextlib import AsyncExitStack, asynccontextmanager

_ROOT = str(pathlib.Path(__file__).resolve().parents[3])

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from servicedesk_agent.infra.monitor.telemetry import trace_scope
from servicedesk_agent.infra.logging import configure_logging
from servicedesk_agent.api.container import AppContainer
from servicedesk_agent.api.deps import env_flag
from servicedesk_agent.api.routes import memory, approvals, auth, chat, conversations, knowledge, ops
from servicedesk_agent.infra.env import env_float
from servicedesk_agent.infra.events import InMemoryEventBus, RedisEventBus
from servicedesk_agent.infra.retention import RetentionPolicy, RetentionSweeper
from servicedesk_agent.agent.request_recall import RequestRecall
from servicedesk_agent.infra.session_revocation import SessionRevocations
from servicedesk_agent.infra import tenant_context
from servicedesk_agent.infra.tenancy import TenantRegistry
from servicedesk_agent.infra.rate_limit import MemoryRateLimiter, RedisRateLimiter, rules_from_env
from servicedesk_agent.infra.protocols import APPLICATION
from servicedesk_agent.infra.identity import dev_mode_allowed
from servicedesk_agent.agent.tooling.knowledge_base import KNOWLEDGE_SEARCH_SCHEMA

load_dotenv(pathlib.Path(_ROOT) / ".env")
configure_logging("api")
logger = logging.getLogger("servicedesk_agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = app.state.container
    from servicedesk_agent.agent.agent_orchestrator import AgentOrchestrator
    from servicedesk_agent.agent.spec import default_spec
    from servicedesk_agent.agent.skill_loader import SkillManager
    from servicedesk_agent.agent.memory.conversation_memory import MemoryManager
    from servicedesk_agent.infra.monitor.telemetry import setup_tracing, shutdown_tracing
    from servicedesk_agent.agent.tooling.knowledge_base import KnowledgeBase
    from servicedesk_agent.agent.tooling.tool_manager import ToolManager, Tool
    from servicedesk_agent.workflow.worker import ExecutionWorker, configured_store, database_url

    from servicedesk_agent.infra.turn_records import build_sha
    sha = build_sha()
    state.lifecycle = APPLICATION.start()
    # Startup and every background loop started here (workers, pumps, sweeps) span tenants: system
    # scope. Requests do not inherit it; each enters its caller's tenant (servicedesk_agent.api.deps, ADR 0023).
    async with AsyncExitStack() as resources:
        resources.enter_context(tenant_context.system_scope())
        resources.callback(state.lifecycle.send, "close")
        services = AsyncExitStack()
        try:
            state.tracing = setup_tracing()
            resources.callback(shutdown_tracing, state.tracing)
            db_url = database_url(os.getenv("SERVICEDESK_AGENT_DATA_DIR", str(pathlib.Path(_ROOT) / "data")))
            state.workflow, state.registry = await asyncio.to_thread(configured_store, db_url)
            resources.push_async_callback(state.workflow.aclose)
            for adapter in state.registry.all():
                if hasattr(adapter, "close"):
                    resources.push_async_callback(adapter.close)
            from servicedesk_agent.infra.change_log import record_deploy
            await state.workflow.run_with_engine(record_deploy, sha)
            state.skills = SkillManager(str(pathlib.Path(_ROOT) / default_spec().skills.root), change_engine=state.workflow.engine)
            state.skills.load()
            from servicedesk_agent.agent.model_router import ModelRouter
            model_router = ModelRouter(engine=state.workflow.engine)
            resources.push_async_callback(model_router.aclose)
            state.tool_manager = ToolManager(api_key="", router=model_router)
            # 审批、执行与向量（知识库、记忆）同库：本地 SQLite，部署 PostgreSQL + pgvector。
            state.kb = KnowledgeBase(db_url)

            async def knowledge(params, ctx=None):
                return await state.kb.search_handler(params, ctx)

            state.tool_manager.register(Tool(
                name="knowledge_search", description="IT 知识库混合检索", handler=knowledge, schema=KNOWLEDGE_SEARCH_SCHEMA,
                cache_ttl=60, timeout_s=8, fallback=lambda p, c, e: {"items": [], "fallback": True, "error": e},
            ))
            state.orchestrator = AgentOrchestrator(skill_manager=state.skills, rag_tool_manager=state.tool_manager, router=model_router,
                directory=state.workflow, knowledge_version=state.kb.collection_version)
            try:
                state.memory = MemoryManager(redis_url=os.getenv("REDIS_URL", "redis://:servicedesk-agent123@127.0.0.1:6379/0"), database_url=db_url, router=model_router)
                resources.push_async_callback(state.memory.close)
                # Redis 属于正确性边界：锁和幂等不可省略，仅构造惰性客户端不足以证明可用。
                await state.memory.ping()
            except Exception as exc:
                state.memory = None
                if os.getenv("SERVICEDESK_AGENT_REQUIRE_REDIS", "true").lower() not in {"0", "false", "no"}:
                    logger.error("Redis is required for safe startup: %s", type(exc).__name__)
                    raise RuntimeError("Redis 不可用，拒绝启动：会话锁和幂等保护不能被绕过") from exc
                logger.warning("memory degraded by explicit SERVICEDESK_AGENT_REQUIRE_REDIS=false: %s", type(exc).__name__)
            if env_flag("SERVICEDESK_AGENT_EMBED_WORKER"):
                state.worker = ExecutionWorker(state.workflow, state.registry, poll_s=env_float("SERVICEDESK_AGENT_WORKER_POLL_S", 5))
                services.push_async_callback(state.worker.stop)
                state.worker.start()
            state.events = RedisEventBus(state.memory._redis) if state.memory else InMemoryEventBus()
            rules = rules_from_env(os.getenv)
            # Always a limiter, even with the per-identity limit off: tenants have their own (ADR 0035).
            state.rate_limiter = RedisRateLimiter(state.memory._redis, rules) if state.memory else MemoryRateLimiter(rules)
            state.request_recall = RequestRecall(lambda principal: state.workflow.run(state.workflow.requester_items, principal))
            state.sessions = SessionRevocations(state.workflow.database, cache_ttl_s=env_float("SERVICEDESK_AGENT_REVOCATION_CACHE_S", 5))
            state.tenants = TenantRegistry(state.workflow.database, cache_ttl_s=env_float("SERVICEDESK_AGENT_TENANT_CACHE_S", 5))
            state.retention = RetentionSweeper(state.workflow.engine, RetentionPolicy.from_env(), database=state.workflow.database,
                                               interval_s=env_float("SERVICEDESK_AGENT_RETENTION_INTERVAL_S", 3600))
            services.push_async_callback(state.retention.stop)
            if env_flag("SERVICEDESK_AGENT_EMBED_WORKER") and env_flag("SERVICEDESK_AGENT_RETENTION_ENABLED", "true"):
                state.retention.start()
            state.lifecycle.send("ready" if state.memory else "degrade")
            if dev_mode_allowed():
                logger.warning("SERVICEDESK_AGENT_AUTH_MODE=dev：使用本地演示身份签发 token，仅适用于开发与评测")
            logger.info("ServiceDesk Agent ready on port %s", os.getenv("SERVICEDESK_AGENT_PORT", "8001"))
            yield
        finally:
            state.lifecycle.send("stop")
            try:
                await services.aclose()
            finally:
                # Producers stop first; requests drain before any dependency is closed.
                if state.background_tasks:
                    remaining = (max(0, state.shutdown_deadline - time.monotonic()) if state.shutdown_deadline is not None
                                 else env_float("SERVICEDESK_AGENT_SHUTDOWN_GRACE_S", 15))
                    _, pending = await asyncio.wait(state.background_tasks, timeout=remaining)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)


def create_app(container: AppContainer | None = None) -> FastAPI:

    app = FastAPI(title="ServiceDesk Agent", version="0.1.0", lifespan=lifespan)
    app.state.container = container if container is not None else AppContainer()
    _cors_origins = os.getenv("SERVICEDESK_AGENT_CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173")
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in _cors_origins.split(",") if o.strip()], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    @app.middleware("http")
    async def trace_request(request, call_next):
        with trace_scope(request.headers.get("traceparent"), "servicedesk_agent.http") as parent:
            response = await call_next(request)
            response.headers["traceparent"] = parent
            response.headers["X-Trace-Id"] = parent.split("-")[1]
            return response

    for module in (memory, ops, auth, approvals, knowledge, chat, conversations):
        app.include_router(module.router)
    return app
