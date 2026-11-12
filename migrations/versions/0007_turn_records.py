"""Durable turn metadata and behavior change events (ADR 0027)."""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None

turn_records = sa.Table(
    "turn_records", sa.MetaData(),
    sa.Column("request_id", sa.String(200), primary_key=True),
    sa.Column("tenant", sa.String(80), nullable=False),
    sa.Column("owner", sa.String(300), nullable=False),
    sa.Column("channel", sa.String(40), nullable=False),
    sa.Column("conv_id", sa.String(200)),
    sa.Column("task_id", sa.String(64)),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("status", sa.String(30), nullable=False),
    sa.Column("error_code", sa.String(80)),
    sa.Column("outcome", sa.String(30), nullable=False),
    sa.Column("intent", sa.String(80), nullable=False),
    sa.Column("intent_group", sa.String(80), nullable=False),
    sa.Column("intent_source", sa.String(80), nullable=False),
    sa.Column("intent_confidence", sa.Float, nullable=False),
    sa.Column("agent_spec", sa.String(80), nullable=False),
    sa.Column("agent_version", sa.String(120), nullable=False),
    sa.Column("model", sa.String(120), nullable=False),
    sa.Column("route", sa.String(80), nullable=False),
    sa.Column("route_reason", sa.String(80), nullable=False),
    sa.Column("prompt_fp", sa.String(64), nullable=False),
    sa.Column("skill_fp", sa.String(64), nullable=False),
    sa.Column("tools_fp", sa.String(64), nullable=False),
    sa.Column("policy_version", sa.String(100), nullable=False),
    sa.Column("kb_version", sa.String(80), nullable=False),
    sa.Column("build_sha", sa.String(80), nullable=False),
    sa.Column("model_requests", sa.Integer, nullable=False),
    sa.Column("input_tokens", sa.Integer, nullable=False),
    sa.Column("output_tokens", sa.Integer, nullable=False),
    sa.Column("cache_read_tokens", sa.Integer, nullable=False),
    sa.Column("cost_usd", sa.Float),
    sa.Column("latency_ms", sa.Float, nullable=False),
    sa.Column("first_token_ms", sa.Float),
    sa.Column("stage_ms", sa.JSON, nullable=False),
    sa.Column("tools", sa.JSON, nullable=False),
    sa.Column("signals", sa.JSON, nullable=False),
    sa.Column("approval_id", sa.String(200)),
)

change_events = sa.Table(
    "change_events", sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("at", sa.Float, nullable=False),
    sa.Column("tenant", sa.String(80)),
    sa.Column("kind", sa.String(40), nullable=False),
    sa.Column("ref", sa.String(200), nullable=False),
    sa.Column("detail", sa.JSON, nullable=False),
    sa.Column("actor", sa.String(200), nullable=False),
)


def upgrade():
    bind = op.get_bind()
    turn_records.create(bind)
    change_events.create(bind)
    op.create_index('ix_turn_records_tenant_created', 'turn_records', ['tenant', 'created_at'])
    op.create_index('ix_turn_records_task', 'turn_records', ['task_id'])
    op.create_index('ix_change_events_at', 'change_events', ['at'])
    if bind.dialect.name == 'postgresql':
        for table in ('turn_records', 'change_events'):
            op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
            op.execute(f'ALTER TABLE {table} FORCE ROW LEVEL SECURITY')
        op.execute('CREATE POLICY tenant_isolation ON turn_records USING (it_copilot_tenant_visible(tenant)) WITH CHECK (it_copilot_tenant_visible(tenant))')
        op.execute("CREATE POLICY tenant_isolation ON change_events USING (it_copilot_tenant_visible(tenant) OR (tenant IS NULL AND coalesce(current_setting('app.tenant', true), '') <> '')) WITH CHECK (it_copilot_tenant_visible(tenant) OR (tenant IS NULL AND current_setting('app.tenant', true) = '*'))")


def downgrade():
    change_events.drop(op.get_bind())
    turn_records.drop(op.get_bind())
