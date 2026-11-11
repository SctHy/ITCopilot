"""References between tasks, without copied transcripts or permissions."""
from sqlalchemy import Table, Column, String, Float, Index


def define_tables(metadata):
    links = Table('task_links', metadata,
        Column('task_id', String(64), primary_key=True), Column('target_id', String(64), primary_key=True),
        Column('created_by', String(200), nullable=False), Column('created_at', Float, nullable=False))
    Index('ix_task_links_target', links.c.target_id)
    return links
