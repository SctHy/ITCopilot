"""Verified legacy memory ownership, independent of shared tasks."""
from sqlalchemy import Table, Column, String, Float


def define_table(metadata):
    return Table('memory_scope_bindings', metadata,
        Column('legacy_subject', String(200), primary_key=True), Column('tenant', String(80), nullable=False),
        Column('scope_key', String(100), nullable=False), Column('migrated_at', Float, nullable=False))
