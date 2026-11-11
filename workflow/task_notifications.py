"""Persistent, leased delivery outbox. Transports and contacts are injected."""
import asyncio
from sqlalchemy import insert, select, update, or_, and_, func
from core.db import task_subscriptions, task_deliveries, task_notification_cursors, task_events, service_tasks, task_identities, task_todos
from core.identity import principal_from_record
from workflow.tasks import key, TaskConflict, TaskForbidden


class TaskNotifications:
    def __init__(self, tasks, contacts=None, transports=None):
        self.tasks, self.contacts, self.transports = tasks, contacts or {}, transports or {}

    def channels(self, tenant, subject):
        return [channel for channel in self.contacts.get(tenant + ':' + subject, {}) if channel in self.transports]

    def settings(self, task_id, principal):
        with self.tasks.read(task_id, principal) as (db, task):
            subscription = db.execute(select(task_subscriptions).where(task_subscriptions.c.task_id == task_id, task_subscriptions.c.subject == principal.subject)).mappings().first()
            deliveries = db.execute(select(task_deliveries).where(task_deliveries.c.task_id == task_id, task_deliveries.c.recipient == principal.subject).order_by(task_deliveries.c.not_before.desc()).limit(30)).mappings().all()
            return {'channels': self.channels(principal.tenant, principal.subject), 'subscription': dict(subscription) if subscription else None, 'deliveries': [dict(r) for r in deliveries]}

    def subscribe(self, task_id, principal, channel, enabled, escalate_to=None):
        with self.tasks.transaction(task_id, principal, write=False) as (db, task):
            if enabled and channel not in self.channels(principal.tenant, principal.subject):
                raise TaskConflict('管理员尚未为当前身份配置此通知渠道')
            if escalate_to:
                if task['requester'] != principal.subject and not principal.is_admin:
                    raise TaskForbidden('只有申请人或管理员可以设置超时升级对象')
                profile = db.execute(select(task_identities.c.profile).where(task_identities.c.tenant == task['tenant'], task_identities.c.subject == escalate_to)).scalar()
                if not profile or channel not in self.channels(task['tenant'], escalate_to):
                    raise TaskConflict('升级对象需先登录并配置通知地址')
                self.tasks._require(db, task_id, principal_from_record(profile))
            where = (task_subscriptions.c.task_id == task_id) & (task_subscriptions.c.subject == principal.subject)
            latest = db.execute(select(func.max(task_events.c.seq)).where(task_events.c.task_id == task_id)).scalar() or 0
            values = dict(channel=channel, enabled=int(enabled), escalate_to=escalate_to, start_seq=latest)
            if not db.execute(update(task_subscriptions).where(where).values(**values)).rowcount:
                db.execute(insert(task_subscriptions).values(task_id=task_id, subject=principal.subject, **values))

    def collect(self):
        with self.tasks.engine.connect() as db:
            task_ids = db.execute(select(task_subscriptions.c.task_id).where(task_subscriptions.c.enabled == 1).distinct()).scalars().all()
        for task_id in task_ids:
            with self.tasks.transaction(task_id) as (db, task):
                cursor = db.execute(select(task_notification_cursors).where(task_notification_cursors.c.task_id == task_id)).mappings().first()
                after = cursor['seq'] if cursor else 0
                rows = db.execute(select(task_events).where(task_events.c.task_id == task_id, task_events.c.seq > after).order_by(task_events.c.seq).limit(100)).mappings().all()
                subs = db.execute(select(task_subscriptions).where(task_subscriptions.c.task_id == task_id, task_subscriptions.c.enabled == 1)).mappings().all()
                for event in rows:
                    for sub in subs:
                        if event['seq'] <= sub['start_seq'] or event['actor'] == sub['subject']:
                            continue
                        recipients = [(sub['subject'], 0)]
                        if event['kind'] in {'reminder', 'approval_reminder'} and sub['escalate_to']:
                            recipients.append((sub['escalate_to'], 1))
                        for recipient, escalated in recipients:
                            delivery_id = key('delivery', event['id'], recipient, sub['channel'])
                            if not db.execute(select(task_deliveries.c.id).where(task_deliveries.c.id == delivery_id)).first():
                                db.execute(insert(task_deliveries).values(id=delivery_id, task_id=task_id, event_id=event['id'], recipient=recipient,
                                    channel=sub['channel'], escalated=escalated, status='queued', attempts=0, version=0, not_before=self.tasks.clock()))
                if rows:
                    if cursor:
                        db.execute(update(task_notification_cursors).where(task_notification_cursors.c.task_id == task_id).values(seq=rows[-1]['seq']))
                    else:
                        db.execute(insert(task_notification_cursors).values(task_id=task_id, seq=rows[-1]['seq']))

    def claim(self):
        now = self.tasks.clock()
        due = or_(and_(task_deliveries.c.status == 'queued', task_deliveries.c.not_before <= now), and_(task_deliveries.c.status == 'sending', task_deliveries.c.lease_until <= now))
        with self.tasks.engine.begin() as db:
            rows = db.execute(select(task_deliveries).where(due).order_by(task_deliveries.c.not_before).limit(20)).mappings().all()
            for row in rows:
                if row['attempts'] >= 5:
                    db.execute(update(task_deliveries).where(task_deliveries.c.id == row['id'], task_deliveries.c.version == row['version'], due)
                        .values(status='failed', lease_until=None, error='retry_limit'))
                    continue
                if db.execute(update(task_deliveries).where(task_deliveries.c.id == row['id'], task_deliveries.c.version == row['version'], due)
                    .values(status='sending', attempts=row['attempts'] + 1, version=row['version'] + 1, lease_until=now + 60)).rowcount:
                    return {**row, 'version': row['version'] + 1, 'attempts': row['attempts'] + 1}

    def message(self, delivery):
        with self.tasks.engine.connect() as db:
            task = db.execute(select(service_tasks).where(service_tasks.c.id == delivery['task_id'])).mappings().one()
            profile = db.execute(select(task_identities.c.profile).where(task_identities.c.tenant == task['tenant'], task_identities.c.subject == delivery['recipient'])).scalar()
            if not profile:
                return None
            try:
                self.tasks._require(db, task['id'], principal_from_record(profile))
            except TaskForbidden:
                return None
            # Unsubscribe also cancels queued normal deliveries.
            subs = db.execute(select(task_subscriptions).where(task_subscriptions.c.task_id == task['id'], task_subscriptions.c.enabled == 1,
                task_subscriptions.c.channel == delivery['channel'])).mappings().all()
            if not any(s['subject'] == delivery['recipient'] or (delivery['escalated'] and s['escalate_to'] == delivery['recipient']) for s in subs):
                return None
            event = db.execute(select(task_events).where(task_events.c.id == delivery['event_id'])).mappings().one()
            todo_id = event['data'].get('todo_id')
            if event['kind'] == 'reminder' and todo_id:
                todo = db.execute(select(task_todos).where(task_todos.c.id == todo_id)).mappings().first()
                if not todo or todo['status'] != 'open' or task['status'] != 'open':
                    return None
            target = self.contacts.get(task['tenant'] + ':' + delivery['recipient'], {}).get(delivery['channel'])
            if not target or delivery['channel'] not in self.transports:
                return None
            return target, ('超时升级：' if delivery['escalated'] else '事项更新：') + task['title'], task['id']

    def settle(self, delivery, status, error=None):
        with self.tasks.engine.begin() as db:
            db.execute(update(task_deliveries).where(task_deliveries.c.id == delivery['id'], task_deliveries.c.version == delivery['version'], task_deliveries.c.status == 'sending', task_deliveries.c.lease_until > self.tasks.clock())
                .values(status=status, error=error, lease_until=None, not_before=self.tasks.clock() + min(300, 2 ** delivery['attempts'])))


class TaskNotificationWorker:
    def __init__(self, service):
        self.service, self.task = service, None

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    @staticmethod
    async def db(fn, *args):
        operation = asyncio.create_task(asyncio.to_thread(fn, *args))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            await asyncio.gather(operation, return_exceptions=True)
            raise

    async def run_once(self):
        await self.db(self.service.collect)
        delivery = await self.db(self.service.claim)
        if not delivery:
            return False
        try:
            message = await self.db(self.service.message, delivery)
            if not message:
                await self.db(self.service.settle, delivery, 'cancelled')
            else:
                await asyncio.wait_for(self.service.transports[delivery['channel']].send(*message, delivery['id']), 25)
                await self.db(self.service.settle, delivery, 'sent')
        except Exception as exc:
            await self.db(self.service.settle, delivery, 'failed' if delivery['attempts'] >= 5 else 'queued', type(exc).__name__)
        return True

    async def loop(self):
        import logging
        while True:
            try:
                if await self.run_once():
                    continue
            except Exception:
                logging.getLogger(__name__).exception('task notification iteration failed')
            await asyncio.sleep(1)
