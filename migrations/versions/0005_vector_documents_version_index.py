"""index for the vector collection version check

``VectorCollection.version()`` runs ``count(*)`` and ``max(updated_at)`` per collection
before every search; this index covers both, so the check no longer scans embedding rows.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_vector_documents_updated", "vector_documents", ["collection", "updated_at"])


def downgrade() -> None:
    op.drop_index("ix_vector_documents_updated", table_name="vector_documents")
