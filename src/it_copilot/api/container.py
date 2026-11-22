from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated
import asyncio
from fastapi import Depends, Request
from it_copilot.infra.events import InMemoryEventBus

if TYPE_CHECKING:
    from it_copilot.agent.agent_orchestrator import AgentOrchestrator
    from it_copilot.agent.memory.conversation_memory import MemoryManager
    from it_copilot.agent.registry import AgentRegistry
    from it_copilot.agent.request_recall import RequestRecall
    from it_copilot.agent.tooling.knowledge_base import KnowledgeBase
    from it_copilot.agent.tooling.mcp_client import StandardMCPClient
    from it_copilot.agent.tooling.tool_manager import MCPToolManager
    from it_copilot.infra.monitor.performance_monitor import PerformanceMonitor
    from it_copilot.infra.retention import RetentionSweeper
    from it_copilot.infra.session_revocation import SessionRevocations
    from it_copilot.agent.skill_loader import SkillManager
    from it_copilot.infra.states import Flow
    from it_copilot.infra.tenancy import TenantRegistry
    from it_copilot.workflow.adapters import AdapterRegistry
    from it_copilot.workflow.store import WorkflowStore
    from it_copilot.workflow.worker import ExecutionWorker
    from it_copilot.infra.rate_limit import MemoryRateLimiter, RedisRateLimiter
    from it_copilot.infra.events import RedisEventBus
    from opentelemetry.sdk.trace import TracerProvider


@dataclass
class AppContainer:
    draining: bool = False
    shutdown_deadline: float | None = None
    agent_registry: AgentRegistry | None = None
    kb: KnowledgeBase | None = None
    lifecycle: Flow | None = None
    mcp_client: StandardMCPClient | None = None
    memory: MemoryManager | None = None
    monitor: PerformanceMonitor | None = None
    orchestrator: AgentOrchestrator | None = None
    rate_limiter: MemoryRateLimiter | RedisRateLimiter | None = None
    registry: AdapterRegistry | None = None
    request_recall: RequestRecall | None = None
    retention: RetentionSweeper | None = None
    sessions: SessionRevocations | None = None
    skills: SkillManager | None = None
    tenants: TenantRegistry | None = None
    tool_manager: MCPToolManager | None = None
    tracing: TracerProvider | None = None
    worker: ExecutionWorker | None = None
    workflow: WorkflowStore | None = None
    events: InMemoryEventBus | RedisEventBus = field(default_factory=InMemoryEventBus)
    background_tasks: set[asyncio.Task] = field(default_factory=set)

    async def run_db(self, operation, *args, **kwargs):
        if self.workflow is None:
            # A partial container may provide independent synchronous identity stores.
            return await asyncio.to_thread(operation, *args, **kwargs)
        return await self.workflow.run(operation, *args, **kwargs)


def get_container(request: Request) -> AppContainer:
    return request.app.state.container


Container = Annotated[AppContainer, Depends(get_container)]
