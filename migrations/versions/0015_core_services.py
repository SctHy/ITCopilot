"""Remove retired collaboration, IM and quality tables."""
from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table("tenants") as batch:
        batch.drop_column("feishu_tenant_key")
    op.drop_index("ix_turn_records_task", table_name="turn_records")
    op.drop_column("turn_records", "task_id")
    op.rename_table("task_identities", "login_identities")
    op.drop_table("task_verifications")
    op.drop_table("task_todos")
    op.drop_table("task_timers")
    op.drop_table("task_suggestions")
    op.drop_table("task_subscriptions")
    op.drop_table("task_runs")
    op.drop_table("task_reviewers")
    op.drop_table("task_reads")
    op.drop_table("task_progress")
    op.drop_table("task_notification_cursors")
    op.drop_table("task_members")
    op.drop_table("task_links")
    op.drop_table("task_goals")
    op.drop_table("task_files")
    op.drop_table("task_events")
    op.drop_table("task_deliveries")
    op.drop_table("task_checkpoints")
    op.drop_table("task_budgets")
    op.drop_table("task_attempts")
    op.drop_table("task_archives")
    op.drop_table("service_tasks")
    op.drop_table("feedback")
    op.drop_table("channel_outbox")
    op.drop_table("channel_inbox")
    op.drop_table("badcases")
    op.drop_table("badcase_mining_cursors")

def downgrade():
    with op.batch_alter_table("tenants") as batch:
        batch.add_column(sa.Column("feishu_tenant_key", sa.String(120)))
        batch.create_unique_constraint("uq_tenants_feishu_tenant_key", ["feishu_tenant_key"])
    op.add_column("turn_records", sa.Column("task_id", sa.String(64)))
    op.create_index("ix_turn_records_task", "turn_records", ["task_id"])
    op.rename_table("login_identities", "task_identities")
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
