"""Application database schema and engine factory (SQLite for local/tests, PostgreSQL in compose).

Holds the approval workflow tables and ``vector_documents`` (knowledge base and memory
embeddings, see ``core/vector_store.py``).

Schema changes go through Alembic (``migrations/``); ``migrate()`` runs at startup so
tests exercise the same migrations that deployments do.

Scheduling columns (``not_before``, ``lease_until``, ``expires_at``) are epoch seconds
so comparisons behave identically on both databases.
"""
from __future__ import annotations

import pathlib
import threading
import weakref

import numpy as np
from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Column,
    Float,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.types import TypeDecorator


metadata = MetaData()

login_identities = Table(
    "login_identities", metadata,
    Column("tenant", String(80), primary_key=True),
    Column("subject", String(200), primary_key=True),
    Column("profile", JSON, nullable=False),
)

approvals = Table(
    "approvals", metadata,
    Column("traceparent", String(128)),
    Column("id", String(200), primary_key=True),
    Column("request_id", String(200), nullable=False),
    Column("conv_id", String(200)),
    Column("requester", String(200), nullable=False),          # Principal.subject
    Column("requester_name", String(200)),                     # display only
    Column("requester_employee_id", String(64)),
    Column("department", String(80)),
    Column("tenant", String(80), nullable=False, default="default"),
    Column("status", String(20), nullable=False),               # draft/pending/approved/rejected/expired/...
    Column("execution", String(20), nullable=False),            # not_executed/queued/executing/succeeded/failed/manual_review
    Column("payload_hash", String(64), nullable=False),
    Column("policy_version", String(80), nullable=False),       # declared label + rule fingerprint
    Column("approvals_required", Integer, nullable=False),
    Column("payload", JSON, nullable=False),                    # frozen action_requests + summary
    Column("version", Integer, nullable=False, default=0),      # optimistic concurrency (CAS)
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("decided_at", Float),
    Column("confirmed_at", Float),                              # requester confirmed the draft
)

Index("ix_approvals_conversation", approvals.c.tenant, approvals.c.requester, approvals.c.conv_id,
      approvals.c.created_at.desc(), approvals.c.id)

approval_decisions = Table(
    "approval_decisions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant", String(80), nullable=False),               # copied from the approval
    Column("approval_id", String(200), nullable=False),
    Column("actor", String(200), nullable=False),
    Column("actor_name", String(200)),                         # display only
    Column("decision", String(20), nullable=False),
    Column("at", Float, nullable=False),
    UniqueConstraint("approval_id", "actor", name="uq_decision_actor"),
)

# Action rows are written in the same transaction as the approval decision and double
# as the outbox: the worker claims due rows with a lease + version CAS.
actions = Table(
    "actions", metadata,
    Column("traceparent", String(128)),
    Column("id", String(64), primary_key=True),                 # idempotency key passed to adapters
    Column("tenant", String(80), nullable=False),               # copied from the approval
    Column("approval_id", String(200), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("kind", String(20), nullable=False),                 # apply / revoke / compensate
    Column("tool_name", String(40), nullable=False),
    Column("resource_key", String(500), nullable=False),
    Column("arguments", JSON, nullable=False),
    Column("adapter", String(40), nullable=False),
    Column("status", String(20), nullable=False),               # queued/executing/unknown/succeeded/failed/cancelled/manual_review
    Column("attempts", Integer, nullable=False, default=0),
    Column("not_before", Float, nullable=False),
    Column("lease_owner", String(80)),
    Column("lease_until", Float),
    Column("version", Integer, nullable=False, default=0),
    Column("result", JSON),
    Column("error", Text),
    Column("parent_action_id", String(64)),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
Index("ix_actions_due", actions.c.status, actions.c.not_before)
Index("ix_actions_approval", actions.c.approval_id, actions.c.seq)
Index("ix_actions_tenant", actions.c.tenant, actions.c.status, actions.c.not_before)
Index("uq_actions_resource_executing", actions.c.resource_key, unique=True,
      sqlite_where=actions.c.status == "executing", postgresql_where=actions.c.status == "executing")

# Ledger of time-bound grants this service created; drives expiry revocation.
access_grants = Table(
    "access_grants", metadata,
    Column("action_id", String(64), primary_key=True),
    Column("tenant", String(80), nullable=False),               # copied from the approval
    Column("approval_id", String(200), nullable=False),
    Column("employee_id", String(64), nullable=False),
    Column("system", String(40), nullable=False),
    Column("role", String(40), nullable=False),
    Column("adapter", String(40), nullable=False),
    Column("baseline_role", String(40)),                        # permission held before this grant
    Column("status", String(20), nullable=False),               # active/preexisting/revoking/revoked/drifted/revoke_failed
    Column("expires_at", Float, nullable=False),
    Column("revoke_action_id", String(64)),
    Column("updated_at", Float, nullable=False),
)
Index("ix_access_grants_approval", access_grants.c.tenant, access_grants.c.approval_id)
Index("ix_access_grants_tenant", access_grants.c.tenant, access_grants.c.employee_id)

audit = Table(
    "audit", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant", String(80), nullable=False),               # copied from the approval
    Column("approval_id", String(200), nullable=False),
    Column("action_id", String(64)),
    Column("event", String(60), nullable=False),
    Column("actor", String(200), nullable=False),
    Column("detail", JSON),
    Column("at", Float, nullable=False),
)
Index("ix_audit_approval", audit.c.approval_id, audit.c.id)
Index("ix_audit_tenant", audit.c.tenant, audit.c.id)

# Administrative changes that are not tied to one approval (knowledge documents, policy
# re-checks, skill reloads, retention sweeps). Append-only, like ``audit`` (enforced by triggers).
# A change writes ``started`` first and ``succeeded``/``failed`` after, with ``ref``
# pointing at the started row. Every entry has an explicit outcome.
admin_audit = Table(
    "admin_audit", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant", String(80), nullable=False),
    Column("actor", String(200), nullable=False),
    Column("event", String(60), nullable=False),
    Column("subject", String(200)),                             # what was changed, e.g. a document id
    Column("detail", JSON),
    Column("at", Float, nullable=False),
    Column("outcome", String(20), nullable=False),              # started / succeeded / failed
    Column("ref", Integer),                                     # the started row this outcome closes
)
Index("ix_admin_audit_tenant", admin_audit.c.tenant, admin_audit.c.id)
Index("ix_admin_audit_ref", admin_audit.c.ref)

# Tokens of (tenant, subject) issued at or before ``revoked_before`` are refused, even though
# their signature and expiry are still valid (offboarding, leaked token, "sign out everywhere").
session_revocations = Table(
    "session_revocations", metadata,
    Column("tenant", String(80), nullable=False),
    Column("subject", String(200), nullable=False),
    Column("revoked_before", Float, nullable=False),
    Column("actor", String(200), nullable=False),
    Column("reason", String(500)),
    Column("at", Float, nullable=False),
    PrimaryKeyConstraint("tenant", "subject"),
)

# Tenant registry (ADR 0022). A token's tenant must be listed here and ``active``. ``issuer`` / ``feishu_tenant_key``
# bind an IdP realm and a Feishu workspace to the tenant; ``quotas`` holds its rate and budget limits
# (servicedesk_agent.infra.quotas). Nothing guesses a tenant.
tenants = Table(
    "tenants", metadata,
    Column("id", String(80), primary_key=True),
    Column("status", String(20), nullable=False),               # active / suspended / offboarding
    Column("tier", String(40), nullable=False),
    Column("issuer", String(300), unique=True),
    Column("quotas", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)

# Turn metadata outlives Redis traces; message bodies remain in short-lived memory.
turn_records = Table(
    "turn_records", metadata,
    Column("request_id", String(200), primary_key=True),
    Column("tenant", String(80), nullable=False),
    Column("owner", String(300), nullable=False),
    Column("channel", String(40), nullable=False),
    Column("conv_id", String(200)),
    Column("created_at", Float, nullable=False),
    Column("status", String(30), nullable=False),
    Column("error_code", String(80)),
    Column("outcome", String(30), nullable=False),
    Column("intent", String(80), nullable=False),
    Column("intent_group", String(80), nullable=False),
    Column("intent_source", String(80), nullable=False),
    Column("intent_confidence", Float, nullable=False),
    Column("agent_spec", String(80), nullable=False),
    Column("agent_version", String(120), nullable=False),
    Column("model", String(120), nullable=False),
    Column("route", String(80), nullable=False),
    Column("route_reason", String(80), nullable=False),
    Column("prompt_fp", String(64), nullable=False),
    Column("skill_fp", String(64), nullable=False),
    Column("tools_fp", String(64), nullable=False),
    Column("policy_version", String(100), nullable=False),
    Column("kb_version", String(80), nullable=False),
    Column("build_sha", String(80), nullable=False),
    Column("model_requests", Integer, nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cache_read_tokens", Integer, nullable=False),
    Column("cost_usd", Float),
    Column("latency_ms", Float, nullable=False),
    Column("first_token_ms", Float),
    Column("stage_ms", JSON, nullable=False),
    Column("tools", JSON, nullable=False),
    Column("signals", JSON, nullable=False),
    Column("quality", JSON, nullable=False, server_default="{}"),
    Column("approval_id", String(200)),
)
Index("ix_turn_records_tenant_created", turn_records.c.tenant, turn_records.c.created_at)

# Private web chat history (core/conversation_history.py). Owned by one (tenant, user)
# scope key and readable only by it. Bodies are redacted before they are written (ADR 0032) and swept
# after SERVICEDESK_AGENT_RETENTION_HISTORY_DAYS (core/retention.py). Not used as Agent context: the Agent
# still reads short-lived working memory, so a reopened old conversation shows its history but the
# model only remembers what working memory still holds.
conversations = Table(
    "conversations", metadata,
    Column("tenant", String(80), nullable=False),
    Column("owner", String(300), nullable=False),
    Column("conv_id", String(200), nullable=False),
    Column("channel", String(40), nullable=False),
    Column("title", String(200), nullable=False),
    Column("message_count", Integer, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    PrimaryKeyConstraint("tenant", "owner", "conv_id", name="pk_conversations"),
)
Index("ix_conversations_owner_updated", conversations.c.tenant, conversations.c.owner, conversations.c.updated_at)

conversation_messages = Table(
    "conversation_messages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant", String(80), nullable=False),
    Column("owner", String(300), nullable=False),
    Column("conv_id", String(200), nullable=False),
    Column("request_id", String(200), nullable=False),
    Column("role", String(20), nullable=False),                 # user / assistant
    Column("content", Text, nullable=False),
    Column("meta", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("tenant", "owner", "request_id", "role", name="uq_conversation_messages_turn"),
)
Index("ix_conversation_messages_conv", conversation_messages.c.tenant, conversation_messages.c.owner,
      conversation_messages.c.conv_id, conversation_messages.c.id)
Index("ix_conversation_messages_created", conversation_messages.c.created_at)

change_events = Table(
    "change_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", Float, nullable=False),
    Column("tenant", String(80)),
    Column("kind", String(40), nullable=False),
    Column("ref", String(200), nullable=False),
    Column("detail", JSON, nullable=False),
    Column("actor", String(200), nullable=False),
)
Index("ix_change_events_at", change_events.c.at)

# ── Local sandbox "directory" system used by the local adapter ───────────────

# Every directory row belongs to a tenant and the tenant is part of the key: the
# sandbox stands in for each tenant's own IAM, so two tenants can both have employee 1001.
directory_employees = Table(
    "directory_employees", metadata,
    Column("tenant", String(80), nullable=False),
    Column("id", String(64), nullable=False),
    Column("username", String(80), nullable=False),
    Column("department", String(80)),
    Column("active", Integer, nullable=False, default=1),
    Column("locked", Integer, nullable=False, default=1),
    Column("password_version", Integer, nullable=False, default=0),
    PrimaryKeyConstraint("tenant", "id", name="pk_directory_employees"),
)

directory_grants = Table(
    "directory_grants", metadata,
    Column("tenant", String(80), nullable=False),
    Column("employee_id", String(64), nullable=False),
    Column("system", String(40), nullable=False),
    Column("role", String(40), nullable=False),
    Column("operation_id", String(64)),
    PrimaryKeyConstraint("tenant", "employee_id", "system", name="pk_directory_grants"),
)

directory_tickets = Table(
    "directory_tickets", metadata,
    Column("tenant", String(80), nullable=False),
    Column("id", String(40), nullable=False),
    Column("employee_id", String(64), nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("status", String(20), nullable=False),
    PrimaryKeyConstraint("tenant", "id", name="pk_directory_tickets"),
)

# Sandbox device inventory: which employee holds which device and whether it is remotely locked.
directory_devices = Table(
    "directory_devices", metadata,
    Column("tenant", String(80), nullable=False),
    Column("id", String(40), nullable=False),                   # asset tag, e.g. DEV-1001
    Column("employee_id", String(64), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("locked", Integer, nullable=False, default=0),
    Column("operation_id", String(64)),                         # action that last locked it
    PrimaryKeyConstraint("tenant", "id", name="pk_directory_devices"),
)

directory_software_inventory = Table(
    "directory_software_inventory", metadata,
    Column("tenant", String(80), primary_key=True),
    Column("software", String(64), primary_key=True),
    Column("total_seats", Integer, nullable=False),
    Column("available_seats", Integer, nullable=False),
)

directory_licenses = Table(
    "directory_licenses", metadata,
    Column("tenant", String(80), primary_key=True),
    Column("employee_id", String(64), primary_key=True),
    Column("software", String(64), primary_key=True),
    Column("scope", String(40), nullable=False),
    Column("active", Integer, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("operation_id", String(64), nullable=False),
)

directory_access_groups = Table(
    "directory_access_groups", metadata,
    Column("tenant", String(80), primary_key=True),
    Column("software", String(64), primary_key=True),
    Column("group_name", String(64), primary_key=True),
    Column("employee_id", String(64), primary_key=True),
    Column("operation_id", String(64), nullable=False),
)

directory_service_health = Table(
    "directory_service_health", metadata,
    Column("tenant", String(80), primary_key=True),
    Column("software", String(64), primary_key=True),
    Column("env", String(64), primary_key=True),
    Column("status", String(40), nullable=False),
    Column("message", Text, nullable=False),
)

# Effects applied by the local adapter, keyed by action id: makes retries idempotent.
directory_effects = Table(
    "directory_effects", metadata,
    Column("tenant", String(80), nullable=False),
    Column("operation_id", String(64), nullable=False),
    Column("result", JSON),
    PrimaryKeyConstraint("operation_id", name="pk_directory_effects"),
)


# ── Embeddings ────────────────────────────────────────────────────────────────

class Embedding(TypeDecorator):
    """pgvector ``vector`` on PostgreSQL; float32 bytes on SQLite, where similarity is computed in Python."""

    impl = LargeBinary
    cache_ok = True

    def load_dialect_impl(self, dialect):
        return dialect.type_descriptor(Vector() if dialect.name == "postgresql" else LargeBinary())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        array = np.asarray(value, dtype=np.float32)
        return array if dialect.name == "postgresql" else array.tobytes()

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return np.asarray(value, dtype=np.float32) if dialect.name == "postgresql" else np.frombuffer(value, dtype=np.float32)


# One table for every collection; ``model`` records which embedding model produced the vector
# so a model change re-embeds old rows instead of mixing vector spaces.
vector_documents = Table(
    "vector_documents", metadata,
    Column("collection", String(80), nullable=False),
    Column("id", String(200), nullable=False),
    Column("document", Text, nullable=False),
    Column("meta", JSON, nullable=False),
    Column("embedding", Embedding, nullable=False),
    Column("model", String(120), nullable=False),
    Column("updated_at", Float, nullable=False),
    PrimaryKeyConstraint("collection", "id", name="pk_vector_documents"),
)
Index("ix_vector_documents_updated", vector_documents.c.collection, vector_documents.c.updated_at)


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"timeout": 15, "check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_connect(dbapi_connection, _record):
            # Let SQLAlchemy own transactions, then take the write lock up front so two
            # read-modify-write transactions serialize instead of deadlocking on upgrade.
            dbapi_connection.isolation_level = None
            dbapi_connection.execute("PRAGMA journal_mode=WAL")
            dbapi_connection.execute("PRAGMA busy_timeout=15000")

        @event.listens_for(engine, "begin")
        def _sqlite_begin(connection):
            # 只读连接（read()）用 DEFERRED，WAL 下与写事务并发；其余事务一开始就拿写锁。
            connection.exec_driver_sql("BEGIN" if connection.get_execution_options().get("read_only") else "BEGIN IMMEDIATE")

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def read(engine: Engine) -> Connection:
    """只读查询用的连接；不要在上面写入。"""
    return engine.connect().execution_options(read_only=True)


def upsert(db: Connection, table: Table):
    """INSERT 支持 on_conflict_do_*；两种支持的方言语法相同，构造器不同。"""
    return (postgresql if db.dialect.name == "postgresql" else sqlite).insert(table)


# 同一进程内同一数据库共用一个连接池、只迁移一次（API 里知识库、记忆、工作流共用）。弱引用：使用方都释放后引擎随之回收，
# 测试里每个用例一个库/ schema 时不会累积连接。
_shared: "weakref.WeakValueDictionary[str, Engine]" = weakref.WeakValueDictionary()
_shared_lock = threading.Lock()


def shared_engine(url: str) -> Engine:
    with _shared_lock:
        engine = _shared.get(url)
        if engine is None:
            migrate(url)
            engine = _shared[url] = make_engine(url)
        return engine


def migrate(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    from servicedesk_agent.infra.tenant_context import system_scope

    root = pathlib.Path(__file__).resolve().parents[3]
    config = Config()
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    with system_scope():  # data migrations span every tenant
        command.upgrade(config, "head")
