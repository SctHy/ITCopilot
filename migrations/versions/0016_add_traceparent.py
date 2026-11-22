"""Persist request trace context across outbox boundaries."""
from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = depends_on = None


def upgrade():
    for table in ("approvals", "actions"):
        op.add_column(table, sa.Column("traceparent", sa.String(128)))


def downgrade():
    for table in ("actions", "approvals"):
        op.drop_column(table, "traceparent")
