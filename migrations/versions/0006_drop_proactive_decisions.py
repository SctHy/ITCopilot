"""drop the proactive decision log (alert-driven ticket intake removed)

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_proactive_created", table_name="proactive_decisions")
    op.drop_table("proactive_decisions")


def downgrade() -> None:
    op.create_table(
        "proactive_decisions",
        sa.Column("id", sa.String(length=200), primary_key=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("fingerprint", sa.String(length=200), nullable=False),
        sa.Column("alertname", sa.String(length=200), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("mode", sa.String(length=20), nullable=False),
        sa.Column("rules_version", sa.String(length=40), nullable=False),
        sa.Column("rule_id", sa.String(length=80), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("approval_id", sa.String(length=200), nullable=True),
        sa.Column("alert", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_proactive_created", "proactive_decisions", ["created_at"])
