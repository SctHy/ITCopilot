"""Bring existing PostgreSQL audit triggers under the new service name."""
from alembic import op


revision = "0017"
down_revision = "0016"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        # Renaming the referenced function preserves all four trigger bindings.
        op.execute("""
            DO $$
            DECLARE audit_function regprocedure;
            BEGIN
                SELECT tgfoid::regprocedure INTO STRICT audit_function
                FROM pg_trigger
                WHERE tgrelid = 'audit'::regclass AND tgname = 'audit_append_only';
                IF audit_function IS DISTINCT FROM to_regprocedure('servicedesk_agent_append_only()') THEN
                    EXECUTE format('ALTER FUNCTION %s RENAME TO servicedesk_agent_append_only',
                                   audit_function);
                END IF;
            END $$;
        """)


def downgrade():
    # The rebranded baseline uses the same function name.
    pass
