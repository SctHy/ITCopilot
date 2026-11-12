"""Feedback and labeled examples are tenant-owned durable evaluation inputs."""
from sqlalchemy import Column, Float, Integer, JSON, String, Table, UniqueConstraint, Index


def define_tables(metadata):
    feedback = Table("feedback", metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("request_id", String(200), nullable=False),
        Column("tenant", String(80), nullable=False),
        Column("actor", String(300), nullable=False),
        Column("rating", Integer, nullable=False),
        Column("reason", String(40), nullable=False),
        Column("comment", String(500), nullable=False),
        Column("created_at", Float, nullable=False),
        UniqueConstraint("request_id", "actor", name="uq_feedback_request_actor"))
    badcases = Table("badcases", metadata,
        Column("id", String(64), primary_key=True),
        Column("request_id", String(200), nullable=False, unique=True),
        Column("tenant", String(80), nullable=False),
        Column("source", String(20), nullable=False),
        Column("signals", JSON, nullable=False),
        Column("status", String(20), nullable=False),
        Column("snapshot", JSON, nullable=False),
        Column("label", JSON, nullable=False),
        Column("labeler", String(300)),
        Column("labeled_at", Float),
        Column("second_label", JSON),
        Column("dataset_case_id", String(80)))
    Index("ix_badcases_tenant_status", badcases.c.tenant, badcases.c.status)
    cursors = Table("badcase_mining_cursors", metadata,
        Column("name", String(40), primary_key=True),
        Column("at", Float, nullable=False))
    return feedback, badcases, cursors
