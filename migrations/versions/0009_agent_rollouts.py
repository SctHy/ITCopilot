"""Versioned Agent rollout state (ADR 0033)."""
from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("agent_rollouts",
        sa.Column("spec_name", sa.String(80), primary_key=True),
        sa.Column("stable_version", sa.String(80), nullable=False),
        sa.Column("canary_version", sa.String(80)),
        sa.Column("canary_percent", sa.Integer, nullable=False),
        sa.Column("canary_tenants", sa.JSON),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("updated_by", sa.String(200), nullable=False),
        sa.CheckConstraint("canary_percent >= 0 AND canary_percent <= 100", name="ck_rollout_percent"))
    # Freeze the version for pre-registry tasks once, rather than guessing during a later run.
    from agents.registry import default_spec
    spec = default_spec()
    events = sa.table("task_events", sa.column("id"), sa.column("seq"), sa.column("data", sa.JSON))
    bind = op.get_bind()
    for row in bind.execute(sa.select(events).where(events.c.seq == 1)).mappings():
        bind.execute(events.update().where(events.c.id == row["id"]).values(
            data={**row["data"], "agent_spec": spec.name, "agent_version": spec.version}))


def downgrade():
    op.drop_table("agent_rollouts")
