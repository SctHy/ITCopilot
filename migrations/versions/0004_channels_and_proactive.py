"""IM channel inbox/outbox and the proactive decision log

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "channel_inbox",
        sa.Column("id", sa.String(length=200), primary_key=True),
        sa.Column("channel", sa.String(length=40), nullable=False),
        sa.Column("conv_id", sa.String(length=200), nullable=False),
        sa.Column("sender", sa.JSON(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("address", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("not_before", sa.Float(), nullable=False),
        sa.Column("lease_until", sa.Float(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(length=200), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )
    op.create_index("ix_channel_inbox_due", "channel_inbox", ["status", "not_before"])
    op.create_index("ix_channel_inbox_request", "channel_inbox", ["request_id"])
    op.create_index("ix_channel_inbox_conv", "channel_inbox", ["conv_id", "created_at"])
    op.create_table(
        "channel_outbox",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("channel", sa.String(length=40), nullable=False),
        sa.Column("conv_key", sa.String(length=200), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("address", sa.JSON(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("ref", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("not_before", sa.Float(), nullable=False),
        sa.Column("lease_until", sa.Float(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("sent_at", sa.Float(), nullable=True),
    )
    op.create_index("ix_channel_outbox_due", "channel_outbox", ["status", "not_before"])
    op.create_index("ix_channel_outbox_conv", "channel_outbox", ["conv_key", "seq"])
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


def downgrade() -> None:
    op.drop_table("proactive_decisions")
    op.drop_table("channel_outbox")
    op.drop_table("channel_inbox")
