"""vector documents for the knowledge base and memory

Embeddings live in the application database. PostgreSQL stores them
as pgvector ``vector`` (the extension is created here); SQLite stores float32 bytes.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    if postgres:
        from pgvector.sqlalchemy import Vector
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "vector_documents",
        sa.Column("collection", sa.String(length=80), nullable=False),
        sa.Column("id", sa.String(length=200), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("embedding", Vector() if postgres else sa.LargeBinary(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("collection", "id", name="pk_vector_documents"),
    )


def downgrade() -> None:
    op.drop_table("vector_documents")
