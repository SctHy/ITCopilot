"""Baseline schema (squashed 2026-11-12; earlier revisions are not upgradeable — recreate the database).

PostgreSQL stores embeddings as pgvector ``vector`` (extension created here); SQLite stores float32 bytes.
"""
from alembic import op
import sqlalchemy as sa

import core.db


revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('access_grants',
    sa.Column('action_id', sa.String(length=64), nullable=False),
    sa.Column('approval_id', sa.String(length=200), nullable=False),
    sa.Column('employee_id', sa.String(length=64), nullable=False),
    sa.Column('system', sa.String(length=40), nullable=False),
    sa.Column('role', sa.String(length=40), nullable=False),
    sa.Column('adapter', sa.String(length=40), nullable=False),
    sa.Column('baseline_role', sa.String(length=40), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('expires_at', sa.Float(), nullable=False),
    sa.Column('revoke_action_id', sa.String(length=64), nullable=True),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('action_id')
    )
    op.create_table('actions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('approval_id', sa.String(length=200), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('tool_name', sa.String(length=40), nullable=False),
    sa.Column('arguments', sa.JSON(), nullable=False),
    sa.Column('adapter', sa.String(length=40), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('not_before', sa.Float(), nullable=False),
    sa.Column('lease_owner', sa.String(length=80), nullable=True),
    sa.Column('lease_until', sa.Float(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('parent_action_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.create_index('ix_actions_approval', ['approval_id', 'seq'], unique=False)
        batch_op.create_index('ix_actions_due', ['status', 'not_before'], unique=False)

    op.create_table('admin_audit',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('event', sa.String(length=60), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('admin_audit', schema=None) as batch_op:
        batch_op.create_index('ix_admin_audit_tenant', ['tenant', 'id'], unique=False)

    op.create_table('approval_decisions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('approval_id', sa.String(length=200), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('actor_name', sa.String(length=200), nullable=True),
    sa.Column('decision', sa.String(length=20), nullable=False),
    sa.Column('at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('approval_id', 'actor', name='uq_decision_actor')
    )
    op.create_table('approvals',
    sa.Column('id', sa.String(length=200), nullable=False),
    sa.Column('request_id', sa.String(length=200), nullable=False),
    sa.Column('conv_id', sa.String(length=200), nullable=True),
    sa.Column('requester', sa.String(length=200), nullable=False),
    sa.Column('requester_name', sa.String(length=200), nullable=True),
    sa.Column('requester_employee_id', sa.String(length=64), nullable=True),
    sa.Column('department', sa.String(length=80), nullable=True),
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('execution', sa.String(length=20), nullable=False),
    sa.Column('payload_hash', sa.String(length=64), nullable=False),
    sa.Column('policy_version', sa.String(length=40), nullable=False),
    sa.Column('approvals_required', sa.Integer(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('expires_at', sa.Float(), nullable=False),
    sa.Column('decided_at', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('audit',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('approval_id', sa.String(length=200), nullable=False),
    sa.Column('action_id', sa.String(length=64), nullable=True),
    sa.Column('event', sa.String(length=60), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('audit', schema=None) as batch_op:
        batch_op.create_index('ix_audit_approval', ['approval_id', 'id'], unique=False)

    op.create_table('channel_inbox',
    sa.Column('id', sa.String(length=200), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('conv_id', sa.String(length=200), nullable=False),
    sa.Column('sender', sa.JSON(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('address', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('not_before', sa.Float(), nullable=False),
    sa.Column('lease_until', sa.Float(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('request_id', sa.String(length=200), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('channel_inbox', schema=None) as batch_op:
        batch_op.create_index('ix_channel_inbox_conv', ['conv_id', 'created_at'], unique=False)
        batch_op.create_index('ix_channel_inbox_due', ['status', 'not_before'], unique=False)
        batch_op.create_index('ix_channel_inbox_request', ['request_id'], unique=False)

    op.create_table('channel_outbox',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('conv_key', sa.String(length=200), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('address', sa.JSON(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('ref', sa.String(length=200), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('not_before', sa.Float(), nullable=False),
    sa.Column('lease_until', sa.Float(), nullable=True),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('sent_at', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('channel_outbox', schema=None) as batch_op:
        batch_op.create_index('ix_channel_outbox_conv', ['conv_key', 'seq'], unique=False)
        batch_op.create_index('ix_channel_outbox_due', ['status', 'not_before'], unique=False)

    op.create_table('directory_devices',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('employee_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('locked', sa.Integer(), nullable=False),
    sa.Column('operation_id', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('directory_effects',
    sa.Column('operation_id', sa.String(length=64), nullable=False),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('operation_id')
    )
    op.create_table('directory_employees',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('username', sa.String(length=80), nullable=False),
    sa.Column('department', sa.String(length=80), nullable=True),
    sa.Column('active', sa.Integer(), nullable=False),
    sa.Column('locked', sa.Integer(), nullable=False),
    sa.Column('password_version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('directory_grants',
    sa.Column('employee_id', sa.String(length=64), nullable=False),
    sa.Column('system', sa.String(length=40), nullable=False),
    sa.Column('role', sa.String(length=40), nullable=False),
    sa.Column('operation_id', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('employee_id', 'system', name='pk_directory_grants')
    )
    op.create_table('directory_tickets',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('employee_id', sa.String(length=64), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('service_tasks',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('requester', sa.String(length=200), nullable=False),
    sa.Column('principal', sa.JSON(), nullable=False),
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('department', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('service_tasks', schema=None) as batch_op:
        batch_op.create_index('ix_service_tasks_access', ['tenant', 'department'], unique=False)

    op.create_table('session_revocations',
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.Column('revoked_before', sa.Float(), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('reason', sa.String(length=500), nullable=True),
    sa.Column('at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('tenant', 'subject')
    )
    op.create_table('task_archives',
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.Column('archived', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('task_id', 'subject')
    )
    op.create_table('task_attempts',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('queued_at', sa.Float(), nullable=False),
    sa.Column('started_at', sa.Float(), nullable=False),
    sa.Column('ended_at', sa.Float(), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('usage', sa.JSON(), nullable=False),
    sa.Column('tools', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('task_attempts', schema=None) as batch_op:
        batch_op.create_index('ix_task_attempts_task', ['task_id', 'started_at'], unique=False)

    op.create_table('task_budgets',
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('token_limit', sa.Integer(), nullable=False),
    sa.Column('run_limit', sa.Integer(), nullable=False),
    sa.Column('used_tokens', sa.Integer(), nullable=False),
    sa.Column('used_runs', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('task_id')
    )
    op.create_table('task_checkpoints',
    sa.Column('run_id', sa.String(length=64), nullable=False),
    sa.Column('step_key', sa.String(length=200), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('input_hash', sa.String(length=64), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('usage', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('run_id', 'step_key')
    )
    with op.batch_alter_table('task_checkpoints', schema=None) as batch_op:
        batch_op.create_index('ix_task_checkpoints_task', ['task_id'], unique=False)

    op.create_table('task_deliveries',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('event_id', sa.String(length=64), nullable=False),
    sa.Column('recipient', sa.String(length=200), nullable=False),
    sa.Column('channel', sa.String(length=30), nullable=False),
    sa.Column('escalated', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('not_before', sa.Float(), nullable=False),
    sa.Column('lease_until', sa.Float(), nullable=True),
    sa.Column('error', sa.String(length=100), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('task_deliveries', schema=None) as batch_op:
        batch_op.create_index('ix_task_delivery_due', ['status', 'not_before'], unique=False)

    op.create_table('task_events',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('actor_name', sa.String(length=200), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('task_id', 'seq', name='uq_task_event_seq')
    )
    op.create_table('task_files',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('uploader', sa.String(length=200), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('mime', sa.String(length=80), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('content', sa.LargeBinary(), nullable=False),
    sa.Column('event_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('task_files', schema=None) as batch_op:
        batch_op.create_index('ix_task_files_task', ['task_id', 'created_at'], unique=False)

    op.create_table('task_goals',
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('checks', sa.JSON(), nullable=False),
    sa.Column('replan_limit', sa.Integer(), nullable=False),
    sa.Column('replans_used', sa.Integer(), nullable=False),
    sa.Column('latest_id', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('task_id')
    )
    op.create_table('task_identities',
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.Column('profile', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('tenant', 'subject')
    )
    op.create_table('task_links',
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('target_id', sa.String(length=64), nullable=False),
    sa.Column('created_by', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('task_id', 'target_id')
    )
    with op.batch_alter_table('task_links', schema=None) as batch_op:
        batch_op.create_index('ix_task_links_target', ['target_id'], unique=False)

    op.create_table('task_members',
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('profile', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('task_id', 'subject')
    )
    op.create_table('task_notification_cursors',
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('task_id')
    )
    op.create_table('task_progress',
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('goal', sa.Text(), nullable=False),
    sa.Column('steps', sa.JSON(), nullable=False),
    sa.Column('blocker', sa.Text(), nullable=False),
    sa.Column('next_action', sa.Text(), nullable=False),
    sa.Column('resolution', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('task_id')
    )
    op.create_table('task_reads',
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('task_id', 'actor')
    )
    op.create_table('task_reviewers',
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.PrimaryKeyConstraint('task_id', 'subject')
    )
    op.create_table('task_runs',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('not_before', sa.Float(), nullable=False),
    sa.Column('lease_until', sa.Float(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('task_runs', schema=None) as batch_op:
        batch_op.create_index('ix_task_runs_due', ['status', 'not_before'], unique=False)
        batch_op.create_index('ix_task_runs_order', ['task_id', 'seq'], unique=False)

    op.create_table('task_subscriptions',
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=False),
    sa.Column('channel', sa.String(length=30), nullable=False),
    sa.Column('enabled', sa.Integer(), nullable=False),
    sa.Column('escalate_to', sa.String(length=200), nullable=True),
    sa.Column('start_seq', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('task_id', 'subject')
    )
    op.create_table('task_suggestions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('base_version', sa.Integer(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('task_timers',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('due_at', sa.Float(), nullable=False),
    sa.Column('wake', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('task_timers', schema=None) as batch_op:
        batch_op.create_index('ix_task_timers_due', ['status', 'due_at'], unique=False)

    op.create_table('task_todos',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('creator', sa.String(length=200), nullable=False),
    sa.Column('assignee', sa.String(length=200), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('answer', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('due_at', sa.Float(), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('reminded_at', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('task_todos', schema=None) as batch_op:
        batch_op.create_index('ix_task_todos_assignee', ['assignee', 'status'], unique=False)

    op.create_table('task_verifications',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('task_id', sa.String(length=64), nullable=False),
    sa.Column('goal_version', sa.Integer(), nullable=False),
    sa.Column('progress_version', sa.Integer(), nullable=False),
    sa.Column('checks', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('lease_until', sa.Float(), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('ended_at', sa.Float(), nullable=True),
    sa.Column('observations', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('task_verifications', schema=None) as batch_op:
        batch_op.create_index('ix_task_verifications_due', ['status', 'created_at'], unique=False)

    op.create_table('vector_documents',
    sa.Column('collection', sa.String(length=80), nullable=False),
    sa.Column('id', sa.String(length=200), nullable=False),
    sa.Column('document', sa.Text(), nullable=False),
    sa.Column('meta', sa.JSON(), nullable=False),
    sa.Column('embedding', core.db.Embedding(), nullable=False),
    sa.Column('model', sa.String(length=120), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('collection', 'id', name='pk_vector_documents')
    )
    with op.batch_alter_table('vector_documents', schema=None) as batch_op:
        batch_op.create_index('ix_vector_documents_updated', ['collection', 'updated_at'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('vector_documents', schema=None) as batch_op:
        batch_op.drop_index('ix_vector_documents_updated')

    op.drop_table('vector_documents')
    with op.batch_alter_table('task_verifications', schema=None) as batch_op:
        batch_op.drop_index('ix_task_verifications_due')

    op.drop_table('task_verifications')
    with op.batch_alter_table('task_todos', schema=None) as batch_op:
        batch_op.drop_index('ix_task_todos_assignee')

    op.drop_table('task_todos')
    with op.batch_alter_table('task_timers', schema=None) as batch_op:
        batch_op.drop_index('ix_task_timers_due')

    op.drop_table('task_timers')
    op.drop_table('task_suggestions')
    op.drop_table('task_subscriptions')
    with op.batch_alter_table('task_runs', schema=None) as batch_op:
        batch_op.drop_index('ix_task_runs_order')
        batch_op.drop_index('ix_task_runs_due')

    op.drop_table('task_runs')
    op.drop_table('task_reviewers')
    op.drop_table('task_reads')
    op.drop_table('task_progress')
    op.drop_table('task_notification_cursors')
    op.drop_table('task_members')
    with op.batch_alter_table('task_links', schema=None) as batch_op:
        batch_op.drop_index('ix_task_links_target')

    op.drop_table('task_links')
    op.drop_table('task_identities')
    op.drop_table('task_goals')
    with op.batch_alter_table('task_files', schema=None) as batch_op:
        batch_op.drop_index('ix_task_files_task')

    op.drop_table('task_files')
    op.drop_table('task_events')
    with op.batch_alter_table('task_deliveries', schema=None) as batch_op:
        batch_op.drop_index('ix_task_delivery_due')

    op.drop_table('task_deliveries')
    with op.batch_alter_table('task_checkpoints', schema=None) as batch_op:
        batch_op.drop_index('ix_task_checkpoints_task')

    op.drop_table('task_checkpoints')
    op.drop_table('task_budgets')
    with op.batch_alter_table('task_attempts', schema=None) as batch_op:
        batch_op.drop_index('ix_task_attempts_task')

    op.drop_table('task_attempts')
    op.drop_table('task_archives')
    op.drop_table('session_revocations')
    with op.batch_alter_table('service_tasks', schema=None) as batch_op:
        batch_op.drop_index('ix_service_tasks_access')

    op.drop_table('service_tasks')
    op.drop_table('directory_tickets')
    op.drop_table('directory_grants')
    op.drop_table('directory_employees')
    op.drop_table('directory_effects')
    op.drop_table('directory_devices')
    with op.batch_alter_table('channel_outbox', schema=None) as batch_op:
        batch_op.drop_index('ix_channel_outbox_due')
        batch_op.drop_index('ix_channel_outbox_conv')

    op.drop_table('channel_outbox')
    with op.batch_alter_table('channel_inbox', schema=None) as batch_op:
        batch_op.drop_index('ix_channel_inbox_request')
        batch_op.drop_index('ix_channel_inbox_due')
        batch_op.drop_index('ix_channel_inbox_conv')

    op.drop_table('channel_inbox')
    with op.batch_alter_table('audit', schema=None) as batch_op:
        batch_op.drop_index('ix_audit_approval')

    op.drop_table('audit')
    op.drop_table('approvals')
    op.drop_table('approval_decisions')
    with op.batch_alter_table('admin_audit', schema=None) as batch_op:
        batch_op.drop_index('ix_admin_audit_tenant')

    op.drop_table('admin_audit')
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.drop_index('ix_actions_due')
        batch_op.drop_index('ix_actions_approval')

    op.drop_table('actions')
    op.drop_table('access_grants')
