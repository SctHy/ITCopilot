"""Task references retain no content snapshots or implicit access grants."""
from alembic import op
import sqlalchemy as sa

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('task_links', sa.Column('task_id', sa.String(64), primary_key=True),
        sa.Column('target_id', sa.String(64), primary_key=True), sa.Column('created_by', sa.String(200), nullable=False),
        sa.Column('created_at', sa.Float, nullable=False))
    op.create_index('ix_task_links_target', 'task_links', ['target_id'])
    op.create_table('memory_scope_bindings', sa.Column('legacy_subject', sa.String(200), primary_key=True),
        sa.Column('tenant', sa.String(80), nullable=False), sa.Column('scope_key', sa.String(100), nullable=False),
        sa.Column('migrated_at', sa.Float, nullable=False))


def downgrade():
    op.drop_table('memory_scope_bindings')
    op.drop_table('task_links')
