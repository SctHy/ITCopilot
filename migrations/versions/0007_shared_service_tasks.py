"""Shared service tasks, event transcript and durable Agent runs."""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("service_tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("requester", sa.String(200), nullable=False),
        sa.Column("principal", sa.JSON(), nullable=False),
        sa.Column("tenant", sa.String(80), nullable=False),
        sa.Column("department", sa.String(80), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False))
    op.create_index("ix_service_tasks_access", "service_tasks", ["tenant", "department"])
    op.create_table("task_events",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("actor_name", sa.String(200), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("task_id", "seq", name="uq_task_event_seq"))
    op.create_table("task_runs",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("not_before", sa.Float(), nullable=False),
        sa.Column("lease_until", sa.Float()),
        sa.Column("error", sa.Text()))
    op.create_index("ix_task_runs_due", "task_runs", ["status", "not_before"])
    op.create_index("ix_task_runs_order", "task_runs", ["task_id", "seq"])


def downgrade():
    for name in ("task_runs", "task_events", "service_tasks"):
        op.drop_table(name)
