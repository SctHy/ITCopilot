"""Sandbox device inventory for remote lock."""
from alembic import op
import sqlalchemy as sa

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('directory_devices',
        sa.Column('id', sa.String(40), primary_key=True),
        sa.Column('employee_id', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(40), nullable=False),
        sa.Column('locked', sa.Integer, nullable=False, server_default='0'),
        sa.Column('operation_id', sa.String(64)))


def downgrade():
    op.drop_table('directory_devices')
