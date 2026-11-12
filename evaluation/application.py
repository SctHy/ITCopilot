"""Isolated evaluation of the SAME application lifespan and HTTP handlers.

Offline uses only a model substitute and fakeredis. Live uses a dedicated Redis
DB/server supplied by the caller. All SQLite files are temporary. Requests
authenticate with dev-mode tokens (default caller: alice; pass ``headers=auth(...)``
for approvers or other identities). No flush
of a user database, external MCP connection, or notification webhook is performed.
"""

import asyncio
import os
import tempfile
import subprocess
from contextlib import ExitStack, asynccontextmanager
from unittest.mock import patch

import httpx2

from core.identity import issue_dev_token


def auth(username: str = "alice", **overrides) -> dict:
    return {"Authorization": "Bearer " + issue_dev_token(username, overrides=overrides or None)}


@asynccontextmanager
async def application(*, model=None, redis_url=None, documents=(), maintenance=False):
    import api.main as api
    from api import state

    with tempfile.TemporaryDirectory(prefix="it-copilot-eval-") as directory:
        env = {
            "IT_COPILOT_DATA_DIR": directory,
            "IT_COPILOT_BUILD_SHA": subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], text=True).strip(),
            "IT_COPILOT_ENV": "development",
            "IT_COPILOT_SEED_DEMO_DOCS": "false",
            "IT_COPILOT_MCP_ENABLED": "false",
            "FEISHU_APP_ID": "",
            "IT_COPILOT_ALERT_WEBHOOK": "",
            "IT_COPILOT_TASK_CONTACTS": "{}",
            "IT_COPILOT_SMTP_HOST": "",
            "IT_COPILOT_AUTH_MODE": "dev",
            "IT_COPILOT_DATABASE_URL": f"sqlite:///{directory}/it_copilot.sqlite3",
            "IT_COPILOT_GITEA_REPOS": "",
            "IT_COPILOT_APPROVAL_WAIT_S": "10",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "",
            "IT_INTENT_LLM_ENABLED": "true",
            # Suites replay many turns as one demo user; they measure behaviour, not abuse protection.
            "IT_COPILOT_RATE_LIMIT_AGENT_TURN": "off",
        }
        if redis_url:
            env["REDIS_URL"] = redis_url
        if model is not None:
            # An offline model substitute must cover every model path, regardless
            # of the developer's .env or installed third-party credentials.
            env.update(IT_INTENT_BACKEND="llm", IT_COPILOT_RERANK_BACKEND="llm")
        with patch.dict(os.environ, env):
            with ExitStack() as stack:
                stack.enter_context(
                    patch.multiple(
                        state,
                        **{
                            name: getattr(state, name)
                            for name in (
                                "orchestrator",
                                "memory",
                                "tool_manager",
                                "kb",
                                "skills",
                                "profile_updater",
                                "tracing",
                                "monitor",
                                "mcp_client",
                                "workflow",
                                "worker",
                                "registry",
                                "channels",
                                "feishu",
                                "events",
                                "tasks",
                                "task_worker",
                                "task_event_pump",
                                "task_capabilities",
                                "task_recall",
                                "request_recall",
                                "sessions",
                                "rate_limiter",
                                "retention",
                            )
                        },
                    )
                )
                stack.enter_context(patch.object(state, "background_tasks", set()))
                if not redis_url:
                    import fakeredis.aioredis

                    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
                    stack.enter_context(
                        patch("memory.conversation_memory.redis.from_url", return_value=fake)
                    )
                async with api.lifespan(api.app):
                    # Async summarization introduces unrelated model calls into measured trials;
                    # long-dialogue suites keep it (maintenance=True) because compression is what they measure.
                    if not maintenance:
                        await state.profile_updater.close()
                        state.profile_updater = None
                    if documents:
                        await state.kb.add_documents_async(list(documents))
                    if model is not None:
                        state.orchestrator._llm = state.orchestrator.agent._llm = model
                        state.orchestrator._intent_recognizer.llm = model
                        state.tool_manager._llm = model
                        state.task_worker.handle.advisor.model = model
                        state.memory._llm = model
                    async with httpx2.AsyncClient(
                        transport=httpx2.ASGITransport(app=api.app),
                        base_url="http://evaluation",
                        headers=auth("alice"),
                        timeout=180,
                    ) as client:
                        yield client, state
                    if state.background_tasks:
                        await asyncio.gather(*list(state.background_tasks), return_exceptions=True)
