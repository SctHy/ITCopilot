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

import numpy as np
from pgvector.sqlalchemy import Vector
from sqlalchemy import (JSON, Column, Float, Index, Integer, LargeBinary, MetaData, PrimaryKeyConstraint, String, Table,
                        Text, UniqueConstraint, create_engine, event)
from sqlalchemy.engine import Engine
from sqlalchemy.types import TypeDecorator

metadata = MetaData()

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

# Effects applied by the local adapter, keyed by action id: makes retries idempotent.
directory_effects = Table(
    "directory_effects", metadata,
    Column("operation_id", String(64), primary_key=True),
    Column("result", JSON),
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
            connection.exec_driver_sql("BEGIN IMMEDIATE")

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def migrate(url: str) -> None:
    from alembic import command
    from alembic.config import Config

    root = pathlib.Path(__file__).resolve().parent.parent
    config = Config()
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
