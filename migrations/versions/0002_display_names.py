"""display names for requester and approvers

Subjects (OIDC `sub`) stay the identity key for authorization and SoD; names are
for humans reading approvals and audit logs.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.add_column(sa.Column("requester_name", sa.String(length=200), nullable=True))
    with op.batch_alter_table("approval_decisions") as batch_op:
        batch_op.add_column(sa.Column("actor_name", sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("approval_decisions") as batch_op:
        batch_op.drop_column("actor_name")
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_column("requester_name")
