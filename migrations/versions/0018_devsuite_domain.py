"""Tenant-scoped DevSuite sandbox directory."""
from alembic import op
from sqlalchemy import Column, Float, Integer, String, Text

revision = "0018"
down_revision = "0017"
branch_labels = depends_on = None


def upgrade():
    op.create_table("directory_software_inventory",
        Column("tenant", String(80), primary_key=True),
        Column("software", String(64), primary_key=True),
        Column("total_seats", Integer, nullable=False),
        Column("available_seats", Integer, nullable=False),
    )
    op.create_table("directory_licenses",
        Column("tenant", String(80), primary_key=True),
        Column("employee_id", String(64), primary_key=True),
        Column("software", String(64), primary_key=True),
        Column("scope", String(40), nullable=False),
        Column("active", Integer, nullable=False),
        Column("expires_at", Float, nullable=False),
        Column("operation_id", String(64), nullable=False),
    )
    op.create_table("directory_access_groups",
        Column("tenant", String(80), primary_key=True),
        Column("software", String(64), primary_key=True),
        Column("group_name", String(64), primary_key=True),
        Column("employee_id", String(64), primary_key=True),
        Column("operation_id", String(64), nullable=False),
    )
    op.create_table("directory_service_health",
        Column("tenant", String(80), primary_key=True),
        Column("software", String(64), primary_key=True),
        Column("env", String(64), primary_key=True),
        Column("status", String(40), nullable=False),
        Column("message", Text, nullable=False),
    )


def downgrade():
    op.drop_table("directory_service_health")
    op.drop_table("directory_access_groups")
    op.drop_table("directory_licenses")
    op.drop_table("directory_software_inventory")
