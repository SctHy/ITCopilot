from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated
import asyncio
from fastapi import Depends, Request
from servicedesk_agent.infra.events import InMemoryEventBus

if TYPE_CHECKING:
    from servicedesk_agent.agent.agent_orchestrator import AgentOrchestrator
    from servicedesk_agent.agent.memory.conversation_memory import MemoryManager
    from servicedesk_agent.agent.request_recall import RequestRecall
    from servicedesk_agent.agent.tooling.knowledge_base import KnowledgeBase
    from servicedesk_agent.agent.tooling.tool_manager import ToolManager
    from servicedesk_agent.infra.retention import RetentionSweeper
    from servicedesk_agent.infra.session_revocation import SessionRevocations
    from servicedesk_agent.agent.skill_loader import SkillManager
    from servicedesk_agent.infra.states import Flow
    from servicedesk_agent.infra.tenancy import TenantRegistry
    from servicedesk_agent.workflow.adapters import AdapterRegistry
    from servicedesk_agent.workflow.store import WorkflowStore
    from servicedesk_agent.workflow.worker import ExecutionWorker
    from servicedesk_agent.infra.rate_limit import MemoryRateLimiter, RedisRateLimiter
    from servicedesk_agent.infra.events import RedisEventBus
    from opentelemetry.sdk.trace import TracerProvider


@dataclass
class AppContainer:
    draining: bool = False
    shutdown_deadline: float | None = None
    kb: KnowledgeBase | None = None
    lifecycle: Flow | None = None
    memory: MemoryManager | None = None
    orchestrator: AgentOrchestrator | None = None
    rate_limiter: MemoryRateLimiter | RedisRateLimiter | None = None
    registry: AdapterRegistry | None = None
    request_recall: RequestRecall | None = None
    retention: RetentionSweeper | None = None
    sessions: SessionRevocations | None = None
    skills: SkillManager | None = None
    tenants: TenantRegistry | None = None
    tool_manager: ToolManager | None = None
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
