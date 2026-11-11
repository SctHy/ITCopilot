"""Task progress, collaboration, reminders and governance. Frozen 0008 schema."""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None
from sqlalchemy import JSON, Column, Float, Integer, MetaData, String, Table, Text, Index


def define_tables(metadata: MetaData):
    progress = Table('task_progress', metadata,
        Column('task_id', String(64), primary_key=True), Column('version', Integer, nullable=False),
        Column('goal', Text, nullable=False), Column('steps', JSON, nullable=False),
        Column('blocker', Text, nullable=False), Column('next_action', Text, nullable=False),
        Column('resolution', Text, nullable=False))
    todos = Table('task_todos', metadata,
        Column('id', String(64), primary_key=True), Column('task_id', String(64), nullable=False),
        Column('creator', String(200), nullable=False), Column('assignee', String(200), nullable=False),
        Column('question', Text, nullable=False), Column('answer', Text, nullable=False),
        Column('status', String(20), nullable=False), Column('due_at', Float),
        Column('created_at', Float, nullable=False), Column('reminded_at', Float))
    timers = Table('task_timers', metadata,
        Column('id', String(64), primary_key=True), Column('task_id', String(64), nullable=False),
        Column('text', Text, nullable=False), Column('due_at', Float, nullable=False),
        Column('wake', Integer, nullable=False), Column('status', String(20), nullable=False))
    reads = Table('task_reads', metadata,
        Column('task_id', String(64), primary_key=True), Column('actor', String(200), primary_key=True),
        Column('seq', Integer, nullable=False))
    budgets = Table('task_budgets', metadata,
        Column('task_id', String(64), primary_key=True), Column('token_limit', Integer, nullable=False),
        Column('run_limit', Integer, nullable=False), Column('used_tokens', Integer, nullable=False),
        Column('used_runs', Integer, nullable=False))
    attempts = Table('task_attempts', metadata,
        Column('id', String(64), primary_key=True), Column('task_id', String(64), nullable=False),
        Column('run_id', String(64), nullable=False), Column('attempt', Integer, nullable=False),
        Column('queued_at', Float, nullable=False), Column('started_at', Float, nullable=False),
        Column('ended_at', Float), Column('status', String(30), nullable=False), Column('error', Text),
        Column('usage', JSON, nullable=False), Column('tools', JSON, nullable=False))
    Index('ix_task_todos_assignee', todos.c.assignee, todos.c.status)
    Index('ix_task_timers_due', timers.c.status, timers.c.due_at)
    Index('ix_task_attempts_task', attempts.c.task_id, attempts.c.started_at)
    return progress, todos, timers, reads, budgets, attempts


def upgrade():
    for table in define_tables(MetaData()):
        table.create(op.get_bind())


def downgrade():
    for table in reversed(define_tables(MetaData())):
        table.drop(op.get_bind())
