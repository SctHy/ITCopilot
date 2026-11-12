"""Persist sandbox ticket state for read-only goal verification."""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("directory_tickets", sa.Column("status", sa.String(20), nullable=False, server_default="open"))
    with op.batch_alter_table("directory_tickets") as batch:
        batch.alter_column("status", existing_type=sa.String(20), server_default=None)


def downgrade():
    with op.batch_alter_table("directory_tickets") as batch:
        batch.drop_column("status")
