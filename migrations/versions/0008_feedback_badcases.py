"""Feedback, frozen badcases and a durable mining cursor (ADR 0029)."""
from alembic import op
from sqlalchemy import Column, Float, Integer, JSON, String, Table, UniqueConstraint, Index, MetaData

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None

metadata = MetaData()
feedback = Table("feedback", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("request_id", String(200), nullable=False),
    Column("tenant", String(80), nullable=False),
    Column("actor", String(300), nullable=False),
    Column("rating", Integer, nullable=False),
    Column("reason", String(40), nullable=False),
    Column("comment", String(500), nullable=False),
    Column("created_at", Float, nullable=False),
    UniqueConstraint("request_id", "actor", name="uq_feedback_request_actor"))
badcases = Table("badcases", metadata,
    Column("id", String(64), primary_key=True),
    Column("request_id", String(200), nullable=False, unique=True),
    Column("tenant", String(80), nullable=False),
    Column("source", String(20), nullable=False),
    Column("signals", JSON, nullable=False),
    Column("status", String(20), nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("label", JSON, nullable=False),
    Column("labeler", String(300)),
    Column("labeled_at", Float),
    Column("second_label", JSON),
    Column("dataset_case_id", String(80)))
Index("ix_badcases_tenant_status", badcases.c.tenant, badcases.c.status)
cursors = Table("badcase_mining_cursors", metadata,
    Column("name", String(40), primary_key=True),
    Column("at", Float, nullable=False))

def upgrade():
    bind = op.get_bind()
    metadata.create_all(bind)
    op.add_column("turn_records", Column("quality", JSON, nullable=False, server_default="{}"))
    if bind.dialect.name == 'postgresql':
        for name in ('feedback', 'badcases'):
            op.execute(f'ALTER TABLE {name} ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE {name} FORCE ROW LEVEL SECURITY')
            op.execute(f'CREATE POLICY tenant_isolation ON {name} USING (it_copilot_tenant_visible(tenant)) WITH CHECK (it_copilot_tenant_visible(tenant))')


def downgrade():
    op.drop_column("turn_records", "quality")
    metadata.drop_all(op.get_bind())
