"""Append-only log of administrative changes not tied to an approval."""
from alembic import op
import sqlalchemy as sa

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('admin_audit',
        sa.Column('id', sa.Integer, primary_key=True, autoincrement=True),
        sa.Column('tenant', sa.String(80), nullable=False),
        sa.Column('actor', sa.String(200), nullable=False),
        sa.Column('event', sa.String(60), nullable=False),
        sa.Column('subject', sa.String(200)),
        sa.Column('detail', sa.JSON),
        sa.Column('at', sa.Float, nullable=False))
    op.create_index('ix_admin_audit_tenant', 'admin_audit', ['tenant', 'id'])


def downgrade():
    op.drop_table('admin_audit')
