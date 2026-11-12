"""Row-level security (ADR 0023). PostgreSQL only; SQLite deployments are single-tenant (ADR 0022).

Every transaction sets ``app.tenant`` from the code's tenant scope (``core.tenant_context`` via the
engine's ``begin`` hook in ``core.db``): the caller's tenant for a request, ``*`` for background jobs
that span tenants, empty outside any scope. The policies:

- tables with a ``tenant`` column: a row is visible and writable when its tenant is the scope's,
  or the scope is ``*``. ``admin_audit`` rows of the global tenant ``*`` (policy changes) are
  readable from every scope.
- ``task_*`` tables: a row is visible when its room (``service_tasks``) is - the subquery is itself
  filtered, so the tenant follows the parent without a copied column.
- an unset or empty ``app.tenant`` matches nothing: code that forgot its scope reads nothing and
  cannot write, instead of reading everything.

Not covered: ``tenants`` (platform registry), ``channel_inbox`` / ``channel_outbox`` (stored before
the sender's tenant is known; platform), ``vector_documents`` (knowledge base, plan phase 2).

Policies do not apply to superusers or roles with BYPASSRLS, whatever FORCE says. Multi-tenant mode
refuses to start under such a role (``core.tenancy.ensure_supported``); run the application as a
plain role that owns the tables (deploy/postgres/app-role.sql).
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

OWN_TENANT = ("approvals", "service_tasks", "session_revocations", "task_identities",
              "actions", "access_grants", "audit", "approval_decisions",
              "directory_employees", "directory_grants", "directory_tickets", "directory_devices", "directory_effects")


def _task_children(bind):
    inspector = sa.inspect(bind)
    return sorted(t for t in inspector.get_table_names()
                  if t.startswith("task_") and t != "task_identities" and "task_id" in {c["name"] for c in inspector.get_columns(t)})


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("""
        CREATE OR REPLACE FUNCTION it_copilot_tenant_visible(row_tenant text) RETURNS boolean
        LANGUAGE sql STABLE AS $$
            SELECT coalesce(current_setting('app.tenant', true), '') IN (row_tenant, '*')
                   AND coalesce(current_setting('app.tenant', true), '') <> ''
        $$
    """)
    for table in OWN_TENANT:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING (it_copilot_tenant_visible(tenant)) "
                   "WITH CHECK (it_copilot_tenant_visible(tenant))")
    op.execute("ALTER TABLE admin_audit ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE admin_audit FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON admin_audit USING (it_copilot_tenant_visible(tenant) OR "
               "(tenant = '*' AND coalesce(current_setting('app.tenant', true), '') <> '')) "
               "WITH CHECK (it_copilot_tenant_visible(tenant))")
    for table in _task_children(bind):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        parent = f"EXISTS (SELECT 1 FROM service_tasks p WHERE p.id = {table}.task_id)"
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({parent}) WITH CHECK ({parent})")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in (*OWN_TENANT, "admin_audit", *_task_children(bind)):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute("DROP FUNCTION IF EXISTS it_copilot_tenant_visible(text)")
