"""IT-Copilot 企业 IT 服务台 API：组装运行时组件（lifespan）并挂载各路由。

路由按业务分组在 api/routes/；运行时组件在 api/state.py；身份与通用依赖在 api/deps.py。
"""
import asyncio
import logging
import os
import pathlib
import sys
from contextlib import AsyncExitStack, asynccontextmanager, suppress

_ROOT = str(pathlib.Path(__file__).parent.parent.resolve())
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api import state
from api.deps import env_flag
from api.routes import approvals, auth, chat, intake, knowledge, ops
from api.routes.chat import _channel_turn
from core.events import InMemoryEventBus, RedisEventBus
from core.identity import dev_mode_allowed
from tooling.knowledge_base import KNOWLEDGE_SEARCH_SCHEMA

load_dotenv(pathlib.Path(_ROOT) / ".env")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("it_copilot")


async def _start_mcp_client():
    """连接 config/mcp_servers.json 中启用的外部 MCP 服务；后台连接，不阻塞启动。"""
    if not env_flag("IT_COPILOT_MCP_ENABLED"):
        return None
    from tooling.mcp_client import StandardMCPClient, load_servers
    configs = load_servers(pathlib.Path(os.getenv("IT_COPILOT_MCP_CONFIG", str(pathlib.Path(_ROOT) / "config/mcp_servers.json"))))
    client = StandardMCPClient(state.tool_manager, configs, on_change=lambda: state.orchestrator.set_external_tools(client.agent_tools()))
    await client.start(wait=False)
    logger.info("external MCP client started: %s", [c.name for c in configs])
    return client


async def _start_channels():
    """飞书机器人：入站消息先落库再处理，回复与审批结果通知走出站 outbox（intake/im.py）。"""
    from dataclasses import replace

    from intake.base import ChannelError
    from intake.feishu import FeishuClient, FeishuSettings, resolve_principal
    from intake.im import ChannelService

    settings = FeishuSettings.from_env()
    if not settings:
        return None, None
    client = FeishuClient(settings)
    if not settings.bot_open_id:
        try:
            client.settings = replace(settings, bot_open_id=await client.bot_open_id())
        except ChannelError as exc:
            logger.warning("飞书机器人 open_id 获取失败，群聊中的 @ 将无法识别（可设置 FEISHU_BOT_OPEN_ID）：%s", exc)
    service = ChannelService(state.workflow.engine, {"feishu": client}, _channel_turn, {"feishu": resolve_principal},
                             poll_s=float(os.getenv("IT_COPILOT_CHANNEL_POLL_S", "1")))
    state.workflow.notify = service.notify_approval
    service.start()
    logger.info("feishu channel enabled (allowed chats: %s)", sorted(settings.allowed_chats) or "all")
    return service, client


@asynccontextmanager
async def lifespan(app: FastAPI):
    from agents.agent_orchestrator import AgentOrchestrator
    from core.concurrency import ProfileUpdater
    from core.skill_loader import SkillManager
    from monitor.telemetry import setup_tracing, shutdown_tracing
    from memory.conversation_memory import MemoryManager
    from monitor.performance_monitor import PerformanceMonitor
    from tooling.knowledge_base import KnowledgeBase
    from tooling.mcp_server import create_http_app, create_server
    from tooling.tool_manager import MCPToolManager, Tool
    from workflow.store import WorkflowStore
    from workflow.worker import ExecutionWorker, build_registry, database_url

    state.tracing = setup_tracing()
    llm = {"api_key": os.getenv("OPENAI_API_KEY", "sk-local"), "base_url": os.getenv("OPENAI_BASE_URL") or None, "model": os.getenv("OPENAI_MODEL", "deepseek-chat")}
    state.skills = SkillManager(os.getenv("IT_COPILOT_SKILLS_DIR", str(pathlib.Path(_ROOT) / "skills")))
    state.skills.load()
    state.tool_manager = MCPToolManager(**llm)
    # 审批、执行与向量（知识库、记忆）同库：本地 SQLite，部署 PostgreSQL + pgvector。
    db_url = database_url(os.getenv("IT_COPILOT_DATA_DIR", str(pathlib.Path(_ROOT) / "data")))
    state.kb = KnowledgeBase(db_url)

    async def knowledge(params, ctx=None):
        return await state.kb.search_handler(params, ctx)

    state.tool_manager.register(Tool(
        name="knowledge_search", description="IT 知识库混合检索", handler=knowledge, schema=KNOWLEDGE_SEARCH_SCHEMA,
        cache_ttl=60, timeout_s=8, fallback=lambda p, c, e: {"items": [], "fallback": True, "error": e},
    ))
    state.orchestrator = AgentOrchestrator(**llm, skill_manager=state.skills, rag_tool_manager=state.tool_manager)
    try:
        state.memory = MemoryManager(redis_url=os.getenv("REDIS_URL", "redis://:it-copilot123@127.0.0.1:6379/0"), database_url=db_url, **llm)
        # Redis 属于正确性边界：锁和幂等不可省略，仅构造惰性客户端不足以证明可用。
        await state.memory.ping()
    except Exception as exc:
        state.memory = None
        if os.getenv("IT_COPILOT_REQUIRE_REDIS", "true").lower() not in {"0", "false", "no"}:
            logger.error("Redis is required for safe startup: %s", type(exc).__name__)
            raise RuntimeError("Redis 不可用，拒绝启动：会话锁和幂等保护不能被绕过") from exc
        logger.warning("memory degraded by explicit IT_COPILOT_REQUIRE_REDIS=false: %s", type(exc).__name__)
    # 审批与执行状态的唯一来源是应用数据库，不放在 Redis 里。
    state.workflow = await asyncio.to_thread(WorkflowStore, db_url)
    state.registry = build_registry(state.workflow)
    state.workflow.route = lambda action: state.registry.for_action(action).name
    state.worker = ExecutionWorker(state.workflow, state.registry, poll_s=float(os.getenv("IT_COPILOT_WORKER_POLL_S", "1")))
    if env_flag("IT_COPILOT_WORKER_ENABLED", "true"):
        state.worker.start()
    state.events = RedisEventBus(state.memory._redis) if state.memory else InMemoryEventBus()
    state.channels, state.feishu = await _start_channels()
    if state.memory:
        # 后台维护（会话压缩 + 画像提炼）走有界 worker，同一用户的积压更新合并为一次。
        state.profile_updater = ProfileUpdater(state.memory, workers=int(os.getenv("IT_COPILOT_MEMORY_WORKERS", "2")))
        state.profile_updater.start()

    async with AsyncExitStack() as stack:
        # 对外 MCP 服务端：/mcp 暴露本地知识库检索。
        mcp_server = create_server(state.kb, state.tool_manager)
        _mcp_mount.app = create_http_app(mcp_server)
        await stack.enter_async_context(mcp_server.session_manager.run())
        state.mcp_client = await _start_mcp_client()
        state.monitor = PerformanceMonitor(state.orchestrator, state.tool_manager, state.mcp_client)
        await state.monitor.start()
        if dev_mode_allowed():
            logger.warning("IT_COPILOT_AUTH_MODE=dev：使用本地演示身份签发 token，仅适用于开发与评测")
        logger.info("IT-Copilot ready on port %s", os.getenv("IT_COPILOT_PORT", "8001"))
        try:
            yield
        finally:
            await state.monitor.stop()
            if state.channels:
                await state.channels.stop()
            if state.feishu:
                await state.feishu.close()
            await state.worker.stop()
            for adapter in state.registry.all():
                if hasattr(adapter, "close"):
                    await adapter.close()
            state.workflow.close()
            if state.mcp_client:
                await state.mcp_client.close()
            _mcp_mount.app = None
    if state.background_tasks:
        await asyncio.gather(*state.background_tasks, return_exceptions=True)
    if state.profile_updater:
        await state.profile_updater.close()
    shutdown_tracing(state.tracing)
    if state.memory:
        with suppress(Exception):
            await state.memory.close()


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
for module in (ops, auth, approvals, knowledge, chat, intake):
    app.include_router(module.router)
