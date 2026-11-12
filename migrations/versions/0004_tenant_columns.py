"""Tenant on every row that can be read without its parent (ADR 0023).

- ``actions``, ``access_grants``, ``audit``, ``approval_decisions``: ``tenant`` copied from the
  approval. The worker reads actions and grants on their own, audit is exported per tenant, and
  row-level security (migration 0005) filters each table by its own column.
- ``directory_*`` (the local sandbox directory): ``tenant`` joins the key, so two tenants can both
  have employee ``1001`` or device ``DEV-1001``. Existing rows belong to ``default``.

The ``audit`` append-only triggers (0002) are dropped for the backfill and recreated.
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

CHILDREN = ("actions", "access_grants", "audit", "approval_decisions")


def _append_only(create: bool) -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    if postgres:
        if create:
            op.execute("CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit FOR EACH ROW EXECUTE FUNCTION it_copilot_append_only()")
            op.execute("CREATE TRIGGER audit_no_truncate BEFORE TRUNCATE ON audit FOR EACH STATEMENT EXECUTE FUNCTION it_copilot_append_only()")
        else:
            op.execute("DROP TRIGGER IF EXISTS audit_append_only ON audit")
            op.execute("DROP TRIGGER IF EXISTS audit_no_truncate ON audit")
        return
    for verb in ("update", "delete"):
        if create:
            op.execute(f"CREATE TRIGGER audit_no_{verb} BEFORE {verb.upper()} ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END")
        else:
            op.execute(f"DROP TRIGGER IF EXISTS audit_no_{verb}")


def _directory_tables(with_tenant: bool):
    """The directory tables keyed with (``with_tenant``) or without the tenant."""
    meta = sa.MetaData()
    key = ["tenant"] if with_tenant else []

    def tenant():
        return [sa.Column("tenant", sa.String(80), nullable=False)] if with_tenant else []
    return [
        sa.Table("directory_employees", meta, *tenant(),
                 sa.Column("id", sa.String(64), nullable=False), sa.Column("username", sa.String(80), nullable=False),
                 sa.Column("department", sa.String(80)), sa.Column("active", sa.Integer, nullable=False),
                 sa.Column("locked", sa.Integer, nullable=False), sa.Column("password_version", sa.Integer, nullable=False),
                 sa.PrimaryKeyConstraint(*key, "id", name="pk_directory_employees")),
        sa.Table("directory_grants", meta, *tenant(),
                 sa.Column("employee_id", sa.String(64), nullable=False), sa.Column("system", sa.String(40), nullable=False),
                 sa.Column("role", sa.String(40), nullable=False), sa.Column("operation_id", sa.String(64)),
                 sa.PrimaryKeyConstraint(*key, "employee_id", "system", name="pk_directory_grants")),
        sa.Table("directory_tickets", meta, *tenant(),
                 sa.Column("id", sa.String(40), nullable=False), sa.Column("employee_id", sa.String(64), nullable=False),
                 sa.Column("title", sa.Text, nullable=False), sa.Column("description", sa.Text, nullable=False),
                 sa.PrimaryKeyConstraint(*key, "id", name="pk_directory_tickets")),
        sa.Table("directory_devices", meta, *tenant(),
                 sa.Column("id", sa.String(40), nullable=False), sa.Column("employee_id", sa.String(64), nullable=False),
                 sa.Column("kind", sa.String(40), nullable=False), sa.Column("locked", sa.Integer, nullable=False),
                 sa.Column("operation_id", sa.String(64)),
                 sa.PrimaryKeyConstraint(*key, "id", name="pk_directory_devices")),
        sa.Table("directory_effects", meta, *tenant(),
                 sa.Column("operation_id", sa.String(64), nullable=False), sa.Column("result", sa.JSON),
                 sa.PrimaryKeyConstraint("operation_id", name="pk_directory_effects")),
    ]


def _rebuild_directory(with_tenant: bool) -> None:
    # Small sandbox tables: copy out, recreate with the new key, copy back. Works the same on
    # SQLite (no primary-key ALTER) and PostgreSQL.
    bind = op.get_bind()
    for table in _directory_tables(with_tenant):
        rows = [dict(r) for r in bind.execute(sa.text(f"SELECT * FROM {table.name}")).mappings()]
        op.drop_table(table.name)
        table.create(bind)
        if rows:
            columns = {c.name for c in table.columns}
            bind.execute(table.insert(), [{**{k: v for k, v in r.items() if k in columns}, **({"tenant": r.get("tenant", "default")} if with_tenant else {})}
                                          for r in rows])


def upgrade() -> None:
    _append_only(create=False)
    for table in CHILDREN:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("tenant", sa.String(80), nullable=True))
        op.execute(f"UPDATE {table} SET tenant = COALESCE((SELECT a.tenant FROM approvals a WHERE a.id = {table}.approval_id), 'default')")
        with op.batch_alter_table(table) as batch:
            batch.alter_column("tenant", existing_type=sa.String(80), nullable=False)
    op.create_index("ix_actions_tenant", "actions", ["tenant", "status", "not_before"])
    op.create_index("ix_access_grants_tenant", "access_grants", ["tenant", "employee_id"])
    op.create_index("ix_audit_tenant", "audit", ["tenant", "id"])
    _append_only(create=True)
    _rebuild_directory(with_tenant=True)


def downgrade() -> None:
    _rebuild_directory(with_tenant=False)
    _append_only(create=False)
    op.drop_index("ix_audit_tenant", table_name="audit")
    op.drop_index("ix_access_grants_tenant", table_name="access_grants")
    op.drop_index("ix_actions_tenant", table_name="actions")
    for table in CHILDREN:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("tenant")
    _append_only(create=True)
