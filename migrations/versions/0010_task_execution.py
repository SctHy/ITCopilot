"""Frozen checkpoint and goal-verification schema."""
from alembic import op
from sqlalchemy import MetaData
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None
from sqlalchemy import Column, String, Integer, Float, JSON, Table, Index


def define_tables(metadata):
    checkpoints = Table('task_checkpoints', metadata,
        Column('run_id', String(64), primary_key=True), Column('step_key', String(200), primary_key=True),
        Column('task_id', String(64), nullable=False), Column('input_hash', String(64), nullable=False),
        Column('payload', JSON, nullable=False), Column('usage', JSON, nullable=False),
        Column('created_at', Float, nullable=False))
    goals = Table('task_goals', metadata,
        Column('task_id', String(64), primary_key=True), Column('version', Integer, nullable=False),
        Column('checks', JSON, nullable=False), Column('replan_limit', Integer, nullable=False),
        Column('replans_used', Integer, nullable=False), Column('latest_id', String(64)))
    verifications = Table('task_verifications', metadata,
        Column('id', String(64), primary_key=True), Column('task_id', String(64), nullable=False),
        Column('goal_version', Integer, nullable=False), Column('progress_version', Integer, nullable=False),
        Column('checks', JSON, nullable=False), Column('status', String(30), nullable=False),
        Column('version', Integer, nullable=False), Column('attempts', Integer, nullable=False),
        Column('lease_until', Float), Column('created_at', Float, nullable=False), Column('ended_at', Float),
        Column('observations', JSON, nullable=False))
    Index('ix_task_checkpoints_task', checkpoints.c.task_id)
    Index('ix_task_verifications_due', verifications.c.status, verifications.c.created_at)
    return checkpoints, goals, verifications


def upgrade():
    for table in define_tables(MetaData()):
        table.create(op.get_bind())

def downgrade():
    for table in reversed(define_tables(MetaData())):
        table.drop(op.get_bind())
