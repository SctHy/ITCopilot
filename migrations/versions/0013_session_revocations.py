"""Per-identity token revocation watermark."""
from alembic import op
import sqlalchemy as sa

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('session_revocations',
        sa.Column('tenant', sa.String(80), nullable=False),
        sa.Column('subject', sa.String(200), nullable=False),
        sa.Column('revoked_before', sa.Float, nullable=False),
        sa.Column('actor', sa.String(200), nullable=False),
        sa.Column('reason', sa.String(500)),
        sa.Column('at', sa.Float, nullable=False),
        sa.PrimaryKeyConstraint('tenant', 'subject'))


def downgrade():
    op.drop_table('session_revocations')
