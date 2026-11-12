"""Serialize target resource mutations, including cross-approval apply/revoke."""
from alembic import op
import sqlalchemy as sa

from workflow.actions import resource_key

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    op.add_column("actions", sa.Column("resource_key", sa.String(500)))
    table = sa.table("actions", sa.column("id"), sa.column("tenant"), sa.column("tool_name"),
                     sa.column("arguments", sa.JSON), sa.column("resource_key"))
    for row in bind.execute(sa.select(table)).mappings():
        key = resource_key(row["tenant"], row["tool_name"], {**row["arguments"], "action_id": row["id"]})
        bind.execute(table.update().where(table.c.id == row["id"]).values(resource_key=key))
    with op.batch_alter_table("actions") as batch:
        batch.alter_column("resource_key", existing_type=sa.String(500), nullable=False)
    op.create_index("uq_actions_resource_executing", "actions", ["resource_key"], unique=True,
                    sqlite_where=sa.text("status = 'executing'"), postgresql_where=sa.text("status = 'executing'"))
    # Revoke targets are now derived from the ledger at execution, never a frozen snapshot.
    for row in bind.execute(sa.select(table).where(table.c.tool_name == "revoke_access")).mappings():
        arguments = {key: value for key, value in row["arguments"].items() if key != "baseline_level"}
        bind.execute(table.update().where(table.c.id == row["id"]).values(arguments=arguments))


def downgrade():
    op.drop_index("uq_actions_resource_executing", table_name="actions")
    with op.batch_alter_table("actions") as batch:
        batch.drop_column("resource_key")
