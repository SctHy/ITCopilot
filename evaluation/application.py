"""Isolated evaluation of the SAME application lifespan and HTTP handlers.

Offline uses only a model substitute and fakeredis. Live uses a dedicated Redis
DB/server supplied by the caller. All Chroma/SQLite files are temporary. Requests
authenticate with dev-mode tokens (default caller: alice; pass ``headers=auth(...)``
for approvers or other identities). No flush
of a user database, external MCP connection, or notification webhook is performed.
"""

import asyncio
import os
import tempfile
from contextlib import ExitStack, asynccontextmanager
from unittest.mock import patch

import httpx

from core.identity import issue_dev_token


def auth(username: str = "alice", **overrides) -> dict:
    return {"Authorization": "Bearer " + issue_dev_token(username, overrides=overrides or None)}


@asynccontextmanager
async def application(*, model=None, redis_url=None, documents=()):
    import api.main as api

    with tempfile.TemporaryDirectory(prefix="it-copilot-eval-") as directory:
        env = {
            "CHROMA_PERSIST_DIRECTORY": directory,
            "CHROMA_PORT": "18998",
            "IT_COPILOT_ENV": "development",
            "IT_COPILOT_SEED_DEMO_DOCS": "false",
            "IT_COPILOT_MCP_ENABLED": "false",
            "IT_COPILOT_ALERT_WEBHOOK": "",
            "IT_COPILOT_AUTH_MODE": "dev",
            "IT_COPILOT_DATABASE_URL": f"sqlite:///{directory}/workflow.sqlite3",
            "IT_COPILOT_GITEA_REPOS": "",
            "IT_COPILOT_APPROVAL_WAIT_S": "10",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "",
            "INTENT_EMBEDDING_PROVIDER": "local",
            "IT_INTENT_LLM_ENABLED": "true",
        }
        if redis_url:
            env["REDIS_URL"] = redis_url
        with patch.dict(os.environ, env):
            with ExitStack() as stack:
                stack.enter_context(
                    patch.multiple(
                        api,
                        **{
                            name: getattr(api, name)
                            for name in (
                                "_orchestrator",
                                "_memory",
                                "_tool_manager",
                                "_kb",
                                "_skills",
                                "_profile_updater",
                                "_tracing",
                                "_monitor",
                                "_mcp_client",
                                "_workflow",
                                "_worker",
                                "_registry",
                                "_events",
                            )
                        },
                    )
                )
                stack.enter_context(patch.object(api, "_background_tasks", set()))
                if not redis_url:
                    import fakeredis.aioredis

                    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
                    stack.enter_context(
                        patch("memory.conversation_memory.redis.from_url", return_value=fake)
                    )
                async with api.lifespan(api.app):
                    # Async summarization introduces unrelated model calls into measured trials.
                    await api._profile_updater.close()
                    api._profile_updater = None
                    if documents:
                        await api._kb.add_documents_async(list(documents))
                    if model is not None:
                        api._orchestrator._intent_recognizer.llm = model
                        api._tool_manager._llm = model
                        api._memory._llm = model
                        for pool in api._orchestrator._pool.values():
                            for agent in pool:
                                agent._llm = model
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=api.app),
                        base_url="http://evaluation",
                        headers=auth("alice"),
                        timeout=180,
                    ) as client:
                        yield client, api
                    if api._background_tasks:
                        await asyncio.gather(*list(api._background_tasks), return_exceptions=True)
