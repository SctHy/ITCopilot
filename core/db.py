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
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.types import TypeDecorator

metadata = MetaData()

# Shared service rooms own durable inputs and bounded Agent runs. Private chat memory
# is never used as their transcript. Approval/action tables retain execution authority.
service_tasks = Table(
    "service_tasks", metadata,
    Column("id", String(64), primary_key=True),
    Column("title", String(200), nullable=False),
    Column("requester", String(200), nullable=False),
    Column("principal", JSON, nullable=False),
    Column("tenant", String(80), nullable=False),
    Column("department", String(80), nullable=False),
    Column("status", String(20), nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
task_events = Table(
    "task_events", metadata,
    Column("id", String(64), primary_key=True),
    Column("task_id", String(64), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("kind", String(30), nullable=False),
    Column("actor", String(200), nullable=False),
    Column("actor_name", String(200), nullable=False),
    Column("text", Text, nullable=False),
    Column("data", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("task_id", "seq", name="uq_task_event_seq"),
)
task_runs = Table(
    "task_runs", metadata,
    Column("id", String(64), primary_key=True),
    Column("task_id", String(64), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("status", String(20), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("version", Integer, nullable=False),
    Column("not_before", Float, nullable=False),
    Column("lease_until", Float),
    Column("error", Text),
)
Index("ix_task_runs_due", task_runs.c.status, task_runs.c.not_before)
Index("ix_task_runs_order", task_runs.c.task_id, task_runs.c.seq)
Index("ix_service_tasks_access", service_tasks.c.tenant, service_tasks.c.department)

from core.task_extensions import define_tables

task_progress, task_todos, task_timers, task_reads, task_budgets, task_attempts = define_tables(metadata)

from core.task_product_schema import define_tables as define_product_tables

task_identities, task_members, task_reviewers, task_archives, task_files, task_suggestions, task_subscriptions, task_notification_cursors, task_deliveries = define_product_tables(metadata)

from core.task_execution_schema import define_tables as define_execution_tables

task_checkpoints, task_goals, task_verifications = define_execution_tables(metadata)

from core.task_history_schema import define_tables as define_history_tables

task_links = define_history_tables(metadata)

approvals = Table(
    "approvals", metadata,
    Column("id", String(200), primary_key=True),
    Column("request_id", String(200), nullable=False),
    Column("conv_id", String(200)),
    Column("requester", String(200), nullable=False),          # Principal.subject
    Column("requester_name", String(200)),                     # display only (migration 0002)
    Column("requester_employee_id", String(64)),
    Column("department", String(80)),
    Column("tenant", String(80), nullable=False, default="default"),
    Column("status", String(20), nullable=False),               # pending/approved/rejected/expired
    Column("execution", String(20), nullable=False),            # not_executed/queued/executing/succeeded/failed/manual_review
    Column("payload_hash", String(64), nullable=False),
    Column("policy_version", String(40), nullable=False),
    Column("approvals_required", Integer, nullable=False),
    Column("payload", JSON, nullable=False),                    # frozen action_requests + summary
    Column("version", Integer, nullable=False, default=0),      # optimistic concurrency (CAS)
    Column("created_at", Float, nullable=False),
    Column("expires_at", Float, nullable=False),
    Column("decided_at", Float),
)

approval_decisions = Table(
    "approval_decisions", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("approval_id", String(200), nullable=False),
    Column("actor", String(200), nullable=False),
    Column("actor_name", String(200)),                         # display only (migration 0002)
    Column("decision", String(20), nullable=False),
    Column("at", Float, nullable=False),
    UniqueConstraint("approval_id", "actor", name="uq_decision_actor"),
)

# Action rows are written in the same transaction as the approval decision and double
# as the outbox: the worker claims due rows with a lease + version CAS.
actions = Table(
    "actions", metadata,
    Column("id", String(64), primary_key=True),                 # idempotency key passed to adapters
    Column("approval_id", String(200), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("kind", String(20), nullable=False),                 # apply / revoke / compensate
    Column("tool_name", String(40), nullable=False),
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

# Ledger of time-bound grants this service created; drives expiry revocation.
access_grants = Table(
    "access_grants", metadata,
    Column("action_id", String(64), primary_key=True),
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

audit = Table(
    "audit", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("approval_id", String(200), nullable=False),
    Column("action_id", String(64)),
    Column("event", String(60), nullable=False),
    Column("actor", String(200), nullable=False),
    Column("detail", JSON),
    Column("at", Float, nullable=False),
)
Index("ix_audit_approval", audit.c.approval_id, audit.c.id)

# Administrative changes that are not tied to one approval (knowledge documents, policy
# re-checks, skill reloads, retention sweeps). Append-only, like ``audit``.
admin_audit = Table(
    "admin_audit", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant", String(80), nullable=False),
    Column("actor", String(200), nullable=False),
    Column("event", String(60), nullable=False),
    Column("subject", String(200)),                             # what was changed, e.g. a document id
    Column("detail", JSON),
    Column("at", Float, nullable=False),
)
Index("ix_admin_audit_tenant", admin_audit.c.tenant, admin_audit.c.id)

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

# ── Local sandbox "directory" system used by the local adapter ───────────────

directory_employees = Table(
    "directory_employees", metadata,
    Column("id", String(64), primary_key=True),
    Column("username", String(80), nullable=False),
    Column("department", String(80)),
    Column("active", Integer, nullable=False, default=1),
    Column("locked", Integer, nullable=False, default=1),
    Column("password_version", Integer, nullable=False, default=0),
)

directory_grants = Table(
    "directory_grants", metadata,
    Column("employee_id", String(64), nullable=False),
    Column("system", String(40), nullable=False),
    Column("role", String(40), nullable=False),
    Column("operation_id", String(64)),
    PrimaryKeyConstraint("employee_id", "system", name="pk_directory_grants"),
)

directory_tickets = Table(
    "directory_tickets", metadata,
    Column("id", String(40), primary_key=True),
    Column("employee_id", String(64), nullable=False),
    Column("title", Text, nullable=False),
    Column("description", Text, nullable=False),
)

# Sandbox device inventory: which employee holds which device and whether it is remotely locked.
directory_devices = Table(
    "directory_devices", metadata,
    Column("id", String(40), primary_key=True),                 # asset tag, e.g. DEV-1001
    Column("employee_id", String(64), nullable=False),
    Column("kind", String(40), nullable=False),
    Column("locked", Integer, nullable=False, default=0),
    Column("operation_id", String(64)),                         # action that last locked it
)

# Effects applied by the local adapter, keyed by action id: makes retries idempotent.
directory_effects = Table(
    "directory_effects", metadata,
    Column("operation_id", String(64), primary_key=True),
    Column("result", JSON),
)


# ── IM channels (intake/) ─────────────────────────────────────────────────────

# Inbound messages are stored before they are processed: the webhook answers within the
# platform's deadline, the primary key drops platform retries, and a crash mid-turn
# leaves a row the next pass picks up again.
channel_inbox = Table(
    "channel_inbox", metadata,
    Column("id", String(200), primary_key=True),                # "<channel>:<platform message id>"
    Column("channel", String(40), nullable=False),
    Column("conv_id", String(200), nullable=False),
    Column("sender", JSON, nullable=False),                     # platform identifiers of the sender
    Column("text", Text, nullable=False),
    Column("address", JSON, nullable=False),                    # where replies go
    Column("status", String(20), nullable=False),               # received/processing/done/ignored/failed
    Column("attempts", Integer, nullable=False, default=0),
    Column("not_before", Float, nullable=False),
    Column("lease_until", Float),
    Column("version", Integer, nullable=False, default=0),
    Column("request_id", String(200)),                          # chat request it produced; approvals point back via it
    Column("error", Text),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
)
Index("ix_channel_inbox_due", channel_inbox.c.status, channel_inbox.c.not_before)
Index("ix_channel_inbox_request", channel_inbox.c.request_id)
Index("ix_channel_inbox_conv", channel_inbox.c.conv_id, channel_inbox.c.created_at)

# Outgoing messages: stored first (in the transaction that caused them), sent by the
# channel service. Messages of one conversation go out in order.
channel_outbox = Table(
    "channel_outbox", metadata,
    Column("id", String(64), primary_key=True),                 # also the platform's idempotency key
    Column("channel", String(40), nullable=False),
    Column("conv_key", String(200), nullable=False),
    Column("seq", Integer, nullable=False),                     # order within the conversation
    Column("address", JSON, nullable=False),
    Column("text", Text, nullable=False),
    Column("ref", String(200)),                                 # inbox id or approval id it belongs to
    Column("status", String(20), nullable=False),               # queued/sending/sent/dropped/failed
    Column("attempts", Integer, nullable=False, default=0),
    Column("not_before", Float, nullable=False),
    Column("lease_until", Float),
    Column("version", Integer, nullable=False, default=0),
    Column("error", Text),
    Column("created_at", Float, nullable=False),
    Column("sent_at", Float),
)
Index("ix_channel_outbox_due", channel_outbox.c.status, channel_outbox.c.not_before)
Index("ix_channel_outbox_conv", channel_outbox.c.conv_key, channel_outbox.c.seq)



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

    root = pathlib.Path(__file__).resolve().parent.parent
    config = Config()
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
