"""Shared test helpers. Auth runs in dev mode: tokens are minted for the demo directory."""
import os
import uuid

import pytest

os.environ.setdefault("IT_COPILOT_AUTH_MODE", "dev")
os.environ.setdefault("IT_COPILOT_ENV", "development")
# 测试不连外部 Jev；需要时显式注入 JevClient。
os.environ["IT_INTENT_BACKEND"] = "llm"


def auth(username: str = "alice", **overrides) -> dict:
    from core.identity import issue_dev_token

    return {"Authorization": "Bearer " + issue_dev_token(username, overrides=overrides or None)}


@pytest.fixture
def db_url(tmp_path):
    """SQLite by default; with IT_COPILOT_TEST_DATABASE_URL each test gets its own PostgreSQL schema.

    ``public`` stays on the search path: the pgvector type and ``<=>`` operator live there.
    """
    base = os.getenv("IT_COPILOT_TEST_DATABASE_URL")
    if not base:
        yield f"sqlite:///{tmp_path / 'app.sqlite3'}"
        return
    from sqlalchemy import create_engine, text
    schema = "t_" + uuid.uuid4().hex[:12]
    admin = create_engine(base)
    with admin.begin() as db:
        db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        db.execute(text(f"CREATE SCHEMA {schema}"))
    yield f"{base}{'&' if '?' in base else '?'}options=-csearch_path%3D{schema},public"
    with admin.begin() as db:
        db.execute(text(f"DROP SCHEMA {schema} CASCADE"))
    admin.dispose()


def with_read_tool(orchestrator):
    """给编排器加一个测试专用的只读工具 diagnose：总是成功，不依赖台账或外部服务。"""
    from tooling.tool_spec import make_tool

    tool = make_tool("diagnose", "测试用只读诊断。", {"code": {"type": "string"}},
                     lambda req, args: {"code": args.get("code", ""), "meaning": "测试诊断结果"}, required=["code"])
    orchestrator.set_shared_tools({**orchestrator._shared_tools, "diagnose": tool})
    return orchestrator
