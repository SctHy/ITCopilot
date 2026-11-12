"""Tenant registry (ADR 0022).

- ``tenants``: the tenants this deployment serves. With ``IT_COPILOT_MULTI_TENANT`` on, a request
  whose token names a tenant that is missing here or not ``active`` is refused; off, the table is
  not consulted and behaviour is unchanged.
- Seeds ``default`` plus every tenant that already owns data (approvals, rooms, identities,
  revocations, admin audit), all ``active``, so switching the flag on later does not lock out
  anyone who could sign in before. The global audit tenant ``*`` is not a tenant.
"""
import time

from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

OWNERS = ("approvals", "service_tasks", "task_identities", "session_revocations", "admin_audit")


def upgrade() -> None:
    tenants = op.create_table(
        'tenants',
        sa.Column('id', sa.String(length=80), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('tier', sa.String(length=40), nullable=False),
        sa.Column('issuer', sa.String(length=300), nullable=True),
        sa.Column('feishu_tenant_key', sa.String(length=120), nullable=True),
        sa.Column('policy_ref', sa.String(length=200), nullable=True),
        sa.Column('quotas', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('issuer'),
        sa.UniqueConstraint('feishu_tenant_key'),
    )
    bind = op.get_bind()
    existing = {"default"}
    for table in OWNERS:
        existing.update(bind.execute(sa.text(f"SELECT DISTINCT tenant FROM {table}")).scalars())
    existing.discard("*")
    now = time.time()
    op.bulk_insert(tenants, [{"id": t, "status": "active", "tier": "standard", "quotas": {}, "created_at": now, "updated_at": now}
                             for t in sorted(existing) if t])


def downgrade() -> None:
    op.drop_table('tenants')
