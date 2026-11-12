"""Audit integrity and requester confirmation.

- ``admin_audit.outcome`` / ``admin_audit.ref``: a change is logged as ``started`` before it runs and
  as ``succeeded`` / ``failed`` (pointing back with ``ref``) after, so a crash in between leaves an
  ``interrupted`` entry instead of nothing. Rows written before this revision have ``outcome`` NULL
  and read as succeeded.
- ``approvals.policy_version`` widened to 80: it now carries the rule fingerprint.
- ``approvals.confirmed_at``: when the requester confirmed a draft and it entered the approver queue.
- ``audit`` and ``admin_audit`` refuse UPDATE / DELETE (and TRUNCATE on PostgreSQL) at the database
  level. This stops application bugs and casual edits; a database owner can still drop the trigger,
  so production should also run the application under a role that only has INSERT / SELECT on them.
"""
from alembic import op
import sqlalchemy as sa


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

APPEND_ONLY = ("audit", "admin_audit")


def upgrade() -> None:
    op.add_column('admin_audit', sa.Column('outcome', sa.String(length=20), nullable=True))
    op.add_column('admin_audit', sa.Column('ref', sa.Integer(), nullable=True))
    op.create_index('ix_admin_audit_ref', 'admin_audit', ['ref'], unique=False)
    op.add_column('approvals', sa.Column('confirmed_at', sa.Float(), nullable=True))
    if op.get_bind().dialect.name == "postgresql":
        # "<declared label>+<12-char rule fingerprint>" (workflow.policy); SQLite does not enforce lengths.
        op.alter_column('approvals', 'policy_version', type_=sa.String(length=80), existing_nullable=False)
        op.execute("""
            CREATE OR REPLACE FUNCTION it_copilot_append_only() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
            END;
            $$ LANGUAGE plpgsql
        """)
        for table in APPEND_ONLY:
            op.execute(f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                       "FOR EACH ROW EXECUTE FUNCTION it_copilot_append_only()")
            op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                       "FOR EACH STATEMENT EXECUTE FUNCTION it_copilot_append_only()")
    else:
        for table in APPEND_ONLY:
            for verb in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                           f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END")


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in APPEND_ONLY:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
            op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute("DROP FUNCTION IF EXISTS it_copilot_append_only()")
    else:
        for table in APPEND_ONLY:
            for verb in ("update", "delete"):
                op.execute(f"DROP TRIGGER IF EXISTS {table}_no_{verb}")
    with op.batch_alter_table('approvals', schema=None) as batch_op:
        batch_op.drop_column('confirmed_at')
    op.drop_index('ix_admin_audit_ref', table_name='admin_audit')
    with op.batch_alter_table('admin_audit', schema=None) as batch_op:
        batch_op.drop_column('ref')
        batch_op.drop_column('outcome')
