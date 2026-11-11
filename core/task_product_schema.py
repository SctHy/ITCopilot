"""Tables owned by task product services."""
from sqlalchemy import Column, String, Integer, Float, JSON, LargeBinary, Table, Index


def define_tables(metadata):
    identities = Table('task_identities', metadata,
        Column('tenant', String(80), primary_key=True), Column('subject', String(200), primary_key=True),
        Column('profile', JSON, nullable=False))
    members = Table('task_members', metadata,
        Column('task_id', String(64), primary_key=True), Column('subject', String(200), primary_key=True),
        Column('role', String(20), nullable=False), Column('profile', JSON, nullable=False))
    reviewers = Table('task_reviewers', metadata,
        Column('task_id', String(64), primary_key=True), Column('subject', String(200), primary_key=True))
    archives = Table('task_archives', metadata,
        Column('task_id', String(64), primary_key=True), Column('subject', String(200), primary_key=True),
        Column('archived', Integer, nullable=False))
    files = Table('task_files', metadata,
        Column('id', String(64), primary_key=True), Column('task_id', String(64), nullable=False),
        Column('uploader', String(200), nullable=False), Column('name', String(200), nullable=False),
        Column('mime', String(80), nullable=False), Column('sha256', String(64), nullable=False),
        Column('size', Integer, nullable=False), Column('content', LargeBinary, nullable=False),
        Column('event_id', String(64), nullable=False), Column('created_at', Float, nullable=False))
    suggestions = Table('task_suggestions', metadata,
        Column('id', String(64), primary_key=True), Column('task_id', String(64), nullable=False),
        Column('base_version', Integer, nullable=False), Column('payload', JSON, nullable=False),
        Column('status', String(20), nullable=False), Column('created_at', Float, nullable=False))
    subscriptions = Table('task_subscriptions', metadata,
        Column('task_id', String(64), primary_key=True), Column('subject', String(200), primary_key=True),
        Column('channel', String(30), nullable=False), Column('enabled', Integer, nullable=False),
        Column('escalate_to', String(200)), Column('start_seq', Integer, nullable=False))
    cursors = Table('task_notification_cursors', metadata,
        Column('task_id', String(64), primary_key=True), Column('seq', Integer, nullable=False))
    deliveries = Table('task_deliveries', metadata,
        Column('id', String(64), primary_key=True), Column('task_id', String(64), nullable=False),
        Column('event_id', String(64), nullable=False), Column('recipient', String(200), nullable=False),
        Column('channel', String(30), nullable=False), Column('escalated', Integer, nullable=False),
        Column('status', String(20), nullable=False), Column('attempts', Integer, nullable=False),
        Column('version', Integer, nullable=False), Column('not_before', Float, nullable=False),
        Column('lease_until', Float), Column('error', String(100)))
    Index('ix_task_files_task', files.c.task_id, files.c.created_at)
    Index('ix_task_delivery_due', deliveries.c.status, deliveries.c.not_before)
    return identities, members, reviewers, archives, files, suggestions, subscriptions, cursors, deliveries
