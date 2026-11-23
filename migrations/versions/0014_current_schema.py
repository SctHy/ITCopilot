"""Current schema baseline; earlier revision chains are deliberately unsupported.

Frozen DDL: future schema changes require a new revision, not edits to this baseline.
Includes append-only audit enforcement and tenant/resource concurrency constraints.
"""
import time
from alembic import op
import sqlalchemy as sa
import servicedesk_agent.infra.db

revision = "0014"
down_revision = None
branch_labels = None
depends_on = None
APPEND_ONLY = ("audit", "admin_audit")


def upgrade():
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table('access_grants',
    sa.Column('action_id', sa.String(length=64), nullable=False),
    sa.Column('tenant', sa.String(length=80), nullable=False),
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
    op.create_index('ix_access_grants_approval', 'access_grants', ['tenant', 'approval_id'], unique=False)
    op.create_index('ix_access_grants_tenant', 'access_grants', ['tenant', 'employee_id'], unique=False)
    op.create_table('actions',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('approval_id', sa.String(length=200), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('tool_name', sa.String(length=40), nullable=False),
    sa.Column('resource_key', sa.String(length=500), nullable=False),
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
    op.create_index('ix_actions_approval', 'actions', ['approval_id', 'seq'], unique=False)
    op.create_index('ix_actions_due', 'actions', ['status', 'not_before'], unique=False)
    op.create_index('ix_actions_tenant', 'actions', ['tenant', 'status', 'not_before'], unique=False)
    op.create_index('uq_actions_resource_executing', 'actions', ['resource_key'], unique=True, sqlite_where=sa.text("status = 'executing'"), postgresql_where=sa.text("status = 'executing'"))
    op.create_table('admin_audit',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('event', sa.String(length=60), nullable=False),
    sa.Column('subject', sa.String(length=200), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('at', sa.Float(), nullable=False),
    sa.Column('outcome', sa.String(length=20), nullable=False),
    sa.Column('ref', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_admin_audit_ref', 'admin_audit', ['ref'], unique=False)
    op.create_index('ix_admin_audit_tenant', 'admin_audit', ['tenant', 'id'], unique=False)
    op.create_table('approval_decisions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tenant', sa.String(length=80), nullable=False),
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
    sa.Column('policy_version', sa.String(length=80), nullable=False),
    sa.Column('approvals_required', sa.Integer(), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('expires_at', sa.Float(), nullable=False),
    sa.Column('decided_at', sa.Float(), nullable=True),
    sa.Column('confirmed_at', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_approvals_conversation', 'approvals', ['tenant', 'requester', 'conv_id', sa.literal_column('created_at DESC'), 'id'], unique=False)
    op.create_table('audit',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('approval_id', sa.String(length=200), nullable=False),
    sa.Column('action_id', sa.String(length=64), nullable=True),
    sa.Column('event', sa.String(length=60), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=True),
    sa.Column('at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_approval', 'audit', ['approval_id', 'id'], unique=False)
    op.create_index('ix_audit_tenant', 'audit', ['tenant', 'id'], unique=False)
    op.create_table('badcase_mining_cursors',
    sa.Column('name', sa.String(length=40), nullable=False),
    sa.Column('at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('name')
    )
    op.create_table('badcases',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('request_id', sa.String(length=200), nullable=False),
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('signals', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('snapshot', sa.JSON(), nullable=False),
    sa.Column('label', sa.JSON(), nullable=False),
    sa.Column('labeler', sa.String(length=300), nullable=True),
    sa.Column('labeled_at', sa.Float(), nullable=True),
    sa.Column('dataset_case_id', sa.String(length=80), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('request_id')
    )
    op.create_index('ix_badcases_tenant_status', 'badcases', ['tenant', 'status'], unique=False)
    op.create_table('change_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('at', sa.Float(), nullable=False),
    sa.Column('tenant', sa.String(length=80), nullable=True),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('ref', sa.String(length=200), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('actor', sa.String(length=200), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_change_events_at', 'change_events', ['at'], unique=False)
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
    op.create_index('ix_channel_inbox_conv', 'channel_inbox', ['conv_id', 'created_at'], unique=False)
    op.create_index('ix_channel_inbox_due', 'channel_inbox', ['status', 'not_before'], unique=False)
    op.create_index('ix_channel_inbox_request', 'channel_inbox', ['request_id'], unique=False)
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
    op.create_index('ix_channel_outbox_conv', 'channel_outbox', ['conv_key', 'seq'], unique=False)
    op.create_index('ix_channel_outbox_due', 'channel_outbox', ['status', 'not_before'], unique=False)
    op.create_table('conversation_messages',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('owner', sa.String(length=300), nullable=False),
    sa.Column('conv_id', sa.String(length=200), nullable=False),
    sa.Column('request_id', sa.String(length=200), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('meta', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant', 'owner', 'request_id', 'role', name='uq_conversation_messages_turn')
    )
    op.create_index('ix_conversation_messages_conv', 'conversation_messages', ['tenant', 'owner', 'conv_id', 'id'], unique=False)
    op.create_index('ix_conversation_messages_created', 'conversation_messages', ['created_at'], unique=False)
    op.create_table('conversations',
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('owner', sa.String(length=300), nullable=False),
    sa.Column('conv_id', sa.String(length=200), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('message_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('tenant', 'owner', 'conv_id', name='pk_conversations')
    )
    op.create_index('ix_conversations_owner_updated', 'conversations', ['tenant', 'owner', 'updated_at'], unique=False)
    op.create_table('directory_devices',
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('employee_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=40), nullable=False),
    sa.Column('locked', sa.Integer(), nullable=False),
    sa.Column('operation_id', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('tenant', 'id', name='pk_directory_devices')
    )
    op.create_table('directory_effects',
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('operation_id', sa.String(length=64), nullable=False),
    sa.Column('result', sa.JSON(), nullable=True),
    sa.PrimaryKeyConstraint('operation_id', name='pk_directory_effects')
    )
    op.create_table('directory_employees',
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('username', sa.String(length=80), nullable=False),
    sa.Column('department', sa.String(length=80), nullable=True),
    sa.Column('active', sa.Integer(), nullable=False),
    sa.Column('locked', sa.Integer(), nullable=False),
    sa.Column('password_version', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('tenant', 'id', name='pk_directory_employees')
    )
    op.create_table('directory_grants',
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('employee_id', sa.String(length=64), nullable=False),
    sa.Column('system', sa.String(length=40), nullable=False),
    sa.Column('role', sa.String(length=40), nullable=False),
    sa.Column('operation_id', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('tenant', 'employee_id', 'system', name='pk_directory_grants')
    )
    op.create_table('directory_tickets',
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('employee_id', sa.String(length=64), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('description', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.PrimaryKeyConstraint('tenant', 'id', name='pk_directory_tickets')
    )
    op.create_table('feedback',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('request_id', sa.String(length=200), nullable=False),
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('actor', sa.String(length=300), nullable=False),
    sa.Column('rating', sa.Integer(), nullable=False),
    sa.Column('reason', sa.String(length=40), nullable=False),
    sa.Column('comment', sa.String(length=500), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('request_id', 'actor', name='uq_feedback_request_actor')
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
    op.create_index('ix_service_tasks_access', 'service_tasks', ['tenant', 'department'], unique=False)
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
    op.create_index('ix_task_attempts_task', 'task_attempts', ['task_id', 'started_at'], unique=False)
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
    op.create_index('ix_task_checkpoints_task', 'task_checkpoints', ['task_id'], unique=False)
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
    op.create_index('ix_task_delivery_due', 'task_deliveries', ['status', 'not_before'], unique=False)
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
    op.create_index('ix_task_files_task', 'task_files', ['task_id', 'created_at'], unique=False)
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
    op.create_index('ix_task_links_target', 'task_links', ['target_id'], unique=False)
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
    op.create_index('ix_task_runs_due', 'task_runs', ['status', 'not_before'], unique=False)
    op.create_index('ix_task_runs_order', 'task_runs', ['task_id', 'seq'], unique=False)
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
    op.create_index('ix_task_timers_due', 'task_timers', ['status', 'due_at'], unique=False)
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
    op.create_index('ix_task_todos_assignee', 'task_todos', ['assignee', 'status'], unique=False)
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
    op.create_index('ix_task_verifications_due', 'task_verifications', ['status', 'created_at'], unique=False)
    op.create_table('tenants',
    sa.Column('id', sa.String(length=80), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('tier', sa.String(length=40), nullable=False),
    sa.Column('issuer', sa.String(length=300), nullable=True),
    sa.Column('feishu_tenant_key', sa.String(length=120), nullable=True),
    sa.Column('quotas', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('feishu_tenant_key'),
    sa.UniqueConstraint('issuer')
    )
    op.create_table('turn_records',
    sa.Column('request_id', sa.String(length=200), nullable=False),
    sa.Column('tenant', sa.String(length=80), nullable=False),
    sa.Column('owner', sa.String(length=300), nullable=False),
    sa.Column('channel', sa.String(length=40), nullable=False),
    sa.Column('conv_id', sa.String(length=200), nullable=True),
    sa.Column('task_id', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('error_code', sa.String(length=80), nullable=True),
    sa.Column('outcome', sa.String(length=30), nullable=False),
    sa.Column('intent', sa.String(length=80), nullable=False),
    sa.Column('intent_group', sa.String(length=80), nullable=False),
    sa.Column('intent_source', sa.String(length=80), nullable=False),
    sa.Column('intent_confidence', sa.Float(), nullable=False),
    sa.Column('agent_spec', sa.String(length=80), nullable=False),
    sa.Column('agent_version', sa.String(length=120), nullable=False),
    sa.Column('model', sa.String(length=120), nullable=False),
    sa.Column('route', sa.String(length=80), nullable=False),
    sa.Column('route_reason', sa.String(length=80), nullable=False),
    sa.Column('prompt_fp', sa.String(length=64), nullable=False),
    sa.Column('skill_fp', sa.String(length=64), nullable=False),
    sa.Column('tools_fp', sa.String(length=64), nullable=False),
    sa.Column('policy_version', sa.String(length=100), nullable=False),
    sa.Column('kb_version', sa.String(length=80), nullable=False),
    sa.Column('build_sha', sa.String(length=80), nullable=False),
    sa.Column('model_requests', sa.Integer(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_read_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=True),
    sa.Column('latency_ms', sa.Float(), nullable=False),
    sa.Column('first_token_ms', sa.Float(), nullable=True),
    sa.Column('stage_ms', sa.JSON(), nullable=False),
    sa.Column('tools', sa.JSON(), nullable=False),
    sa.Column('signals', sa.JSON(), nullable=False),
    sa.Column('quality', sa.JSON(), server_default='{}', nullable=False),
    sa.Column('approval_id', sa.String(length=200), nullable=True),
    sa.PrimaryKeyConstraint('request_id')
    )
    op.create_index('ix_turn_records_task', 'turn_records', ['task_id'], unique=False)
    op.create_index('ix_turn_records_tenant_created', 'turn_records', ['tenant', 'created_at'], unique=False)
    op.create_table('vector_documents',
    sa.Column('collection', sa.String(length=80), nullable=False),
    sa.Column('id', sa.String(length=200), nullable=False),
    sa.Column('document', sa.Text(), nullable=False),
    sa.Column('meta', sa.JSON(), nullable=False),
    sa.Column('embedding', servicedesk_agent.infra.db.Embedding(), nullable=False),
    sa.Column('model', sa.String(length=120), nullable=False),
    sa.Column('updated_at', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('collection', 'id', name='pk_vector_documents')
    )
    op.create_index('ix_vector_documents_updated', 'vector_documents', ['collection', 'updated_at'], unique=False)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION servicedesk_agent_append_only() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION '% is append-only', TG_TABLE_NAME;
            END;
            $$ LANGUAGE plpgsql
        """)
        for table in APPEND_ONLY:
            op.execute(f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
                       "FOR EACH ROW EXECUTE FUNCTION servicedesk_agent_append_only()")
            op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
                       "FOR EACH STATEMENT EXECUTE FUNCTION servicedesk_agent_append_only()")
    else:
        for table in APPEND_ONLY:
            for verb in ("UPDATE", "DELETE"):
                op.execute(f"CREATE TRIGGER {table}_no_{verb.lower()} BEFORE {verb} ON {table} "
                           f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END")

    now = time.time()
    tenants = sa.table("tenants", sa.column("id", sa.String), sa.column("status", sa.String),
                       sa.column("tier", sa.String), sa.column("quotas", sa.JSON),
                       sa.column("created_at", sa.Float), sa.column("updated_at", sa.Float))
    op.bulk_insert(tenants, [{"id": "default", "status": "active", "tier": "standard",
                              "quotas": {}, "created_at": now, "updated_at": now}])


def downgrade():
    op.drop_index('ix_vector_documents_updated', table_name='vector_documents')
    op.drop_table('vector_documents')
    op.drop_index('ix_turn_records_tenant_created', table_name='turn_records')
    op.drop_index('ix_turn_records_task', table_name='turn_records')
    op.drop_table('turn_records')
    op.drop_table('tenants')
    op.drop_index('ix_task_verifications_due', table_name='task_verifications')
    op.drop_table('task_verifications')
    op.drop_index('ix_task_todos_assignee', table_name='task_todos')
    op.drop_table('task_todos')
    op.drop_index('ix_task_timers_due', table_name='task_timers')
    op.drop_table('task_timers')
    op.drop_table('task_suggestions')
    op.drop_table('task_subscriptions')
    op.drop_index('ix_task_runs_order', table_name='task_runs')
    op.drop_index('ix_task_runs_due', table_name='task_runs')
    op.drop_table('task_runs')
    op.drop_table('task_reviewers')
    op.drop_table('task_reads')
    op.drop_table('task_progress')
    op.drop_table('task_notification_cursors')
    op.drop_table('task_members')
    op.drop_index('ix_task_links_target', table_name='task_links')
    op.drop_table('task_links')
    op.drop_table('task_identities')
    op.drop_table('task_goals')
    op.drop_index('ix_task_files_task', table_name='task_files')
    op.drop_table('task_files')
    op.drop_table('task_events')
    op.drop_index('ix_task_delivery_due', table_name='task_deliveries')
    op.drop_table('task_deliveries')
    op.drop_index('ix_task_checkpoints_task', table_name='task_checkpoints')
    op.drop_table('task_checkpoints')
    op.drop_table('task_budgets')
    op.drop_index('ix_task_attempts_task', table_name='task_attempts')
    op.drop_table('task_attempts')
    op.drop_table('task_archives')
    op.drop_table('session_revocations')
    op.drop_index('ix_service_tasks_access', table_name='service_tasks')
    op.drop_table('service_tasks')
    op.drop_table('feedback')
    op.drop_table('directory_tickets')
    op.drop_table('directory_grants')
    op.drop_table('directory_employees')
    op.drop_table('directory_effects')
    op.drop_table('directory_devices')
    op.drop_index('ix_conversations_owner_updated', table_name='conversations')
    op.drop_table('conversations')
    op.drop_index('ix_conversation_messages_created', table_name='conversation_messages')
    op.drop_index('ix_conversation_messages_conv', table_name='conversation_messages')
    op.drop_table('conversation_messages')
    op.drop_index('ix_channel_outbox_due', table_name='channel_outbox')
    op.drop_index('ix_channel_outbox_conv', table_name='channel_outbox')
    op.drop_table('channel_outbox')
    op.drop_index('ix_channel_inbox_request', table_name='channel_inbox')
    op.drop_index('ix_channel_inbox_due', table_name='channel_inbox')
    op.drop_index('ix_channel_inbox_conv', table_name='channel_inbox')
    op.drop_table('channel_inbox')
    op.drop_index('ix_change_events_at', table_name='change_events')
    op.drop_table('change_events')
    op.drop_index('ix_badcases_tenant_status', table_name='badcases')
    op.drop_table('badcases')
    op.drop_table('badcase_mining_cursors')
    op.drop_index('ix_audit_tenant', table_name='audit')
    op.drop_index('ix_audit_approval', table_name='audit')
    op.drop_table('audit')
    op.drop_index('ix_approvals_conversation', table_name='approvals')
    op.drop_table('approvals')
    op.drop_table('approval_decisions')
    op.drop_index('ix_admin_audit_tenant', table_name='admin_audit')
    op.drop_index('ix_admin_audit_ref', table_name='admin_audit')
    op.drop_table('admin_audit')
    op.drop_index('uq_actions_resource_executing', table_name='actions', sqlite_where=sa.text("status = 'executing'"), postgresql_where=sa.text("status = 'executing'"))
    op.drop_index('ix_actions_tenant', table_name='actions')
    op.drop_index('ix_actions_due', table_name='actions')
    op.drop_index('ix_actions_approval', table_name='actions')
    op.drop_table('actions')
    op.drop_index('ix_access_grants_tenant', table_name='access_grants')
    op.drop_index('ix_access_grants_approval', table_name='access_grants')
    op.drop_table('access_grants')

    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS servicedesk_agent_append_only()")
