"""Membership access policy. Invitations grant room access, never organization roles."""
import threading
from collections import OrderedDict

from sqlalchemy import and_, or_, select, insert, update, delete
from sqlalchemy.exc import IntegrityError
from core import db as schema
from core.db import service_tasks, task_members, task_reviewers, task_identities, task_todos, approvals
from core.identity import principal_from_record
from workflow.tasks import TaskForbidden, TaskConflict


class TaskAccess:
    OBSERVED_MAX = 4096

    def __init__(self, tasks):
        self.tasks = tasks
        self._observed, self._observed_lock = OrderedDict(), threading.Lock()

    def observe(self, principal):
        """Record the directory profile; runs per authenticated request, so unchanged profiles skip the DB."""
        ident, profile = (principal.tenant, principal.subject), principal.public()
        with self._observed_lock:
            if self._observed.get(ident) == profile:
                self._observed.move_to_end(ident)
                return
        values = dict(tenant=principal.tenant, subject=principal.subject, profile=profile)
        where = (task_identities.c.tenant == principal.tenant) & (task_identities.c.subject == principal.subject)
        with schema.read(self.tasks.engine) as db:
            stored = db.execute(select(task_identities.c.profile).where(where)).scalar()
        if stored != profile:
            try:
                with self.tasks.engine.begin() as db:
                    if not db.execute(update(task_identities).where(where).values(profile=profile)).rowcount:
                        db.execute(insert(task_identities).values(**values))
            except IntegrityError:
                with self.tasks.engine.begin() as db:
                    db.execute(update(task_identities).where(where).values(profile=profile))
        with self._observed_lock:
            self._observed[ident] = profile
            self._observed.move_to_end(ident)
            while len(self._observed) > self.OBSERVED_MAX:
                self._observed.popitem(last=False)

    def filter(self, principal):
        scope = service_tasks.c.tenant == principal.tenant
        if principal.is_admin:
            return scope
        membership = select(task_members.c.role).where(task_members.c.task_id == service_tasks.c.id, task_members.c.subject == principal.subject).scalar_subquery()
        assigned = select(task_reviewers.c.subject).where(task_reviewers.c.task_id == service_tasks.c.id,
            task_reviewers.c.subject == principal.subject).exists()
        return and_(scope, or_(service_tasks.c.requester == principal.subject,
            membership.in_(['collaborator', 'observer']), and_(membership.is_(None), assigned)))

    def writable(self, db, task, principal):
        if principal.is_admin or task['requester'] == principal.subject:
            return True
        role = db.execute(select(task_members.c.role).where(task_members.c.task_id == task['id'], task_members.c.subject == principal.subject)).scalar()
        return role not in {'observer', 'revoked'}

    @staticmethod
    def manage(task, principal):
        if not principal.is_admin and task['requester'] != principal.subject:
            raise TaskForbidden('只有申请人或管理员可以管理事项成员')

    def directory(self, task_id, principal):
        with self.tasks.read(task_id, principal) as (db, task):
            self.manage(task, principal)
            return [r for r in db.execute(select(task_identities.c.profile).where(task_identities.c.tenant == task['tenant'])).scalars()]

    def profile(self, db, task, subject):
        row = db.execute(select(task_identities.c.profile).where(task_identities.c.tenant == task['tenant'], task_identities.c.subject == subject)).scalar()
        if not row:
            raise TaskForbidden('目标用户需先登录本租户，身份不能由请求体声明')
        return row

    def require_assignee(self, db, task, subject, caller):
        """A historical participant is not necessarily a current writable member."""
        if subject == task['requester'] or (subject == caller.subject and caller.is_admin):
            return
        profile = self.profile(db, task, subject)
        person = principal_from_record(profile)
        visible = db.execute(select(service_tasks.c.id).where(service_tasks.c.id == task['id'], self.filter(person))).first()
        if not visible or not self.writable(db, task, person):
            raise TaskForbidden('只能指派给当前可以参与本事项的人员')

    def list(self, task_id, principal):
        with self.tasks.read(task_id, principal) as (db, task):
            return [dict(r) for r in db.execute(select(task_members).where(task_members.c.task_id == task_id)).mappings()]

    def set_member(self, task_id, principal, subject, role):
        with self.tasks.transaction(task_id, principal) as (db, task):
            self.manage(task, principal)
            if subject == task['requester']:
                raise TaskConflict('不能移除或降级申请人')
            profile = self.profile(db, task, subject)
            if role in {'observer', 'revoked'}:
                if db.execute(select(task_todos.c.id).where(task_todos.c.task_id == task_id, task_todos.c.assignee == subject, task_todos.c.status == 'open')).first():
                    raise TaskConflict('请先取消或改派该成员的未完成待办')
                if db.execute(select(task_reviewers.c.subject).where(task_reviewers.c.task_id == task_id, task_reviewers.c.subject == subject)).first():
                    raise TaskConflict('请先调整指定审批人')
            where = (task_members.c.task_id == task_id) & (task_members.c.subject == subject)
            if not db.execute(update(task_members).where(where).values(role=role, profile=profile)).rowcount:
                db.execute(insert(task_members).values(task_id=task_id, subject=subject, role=role, profile=profile))
            self.tasks.record(db, task, principal, 'member', f"成员 {profile['username']} 的事项身份已设为 {role}。", {'subject': subject, 'role': role})

    def assign(self, task_id, principal, subjects):
        subjects = list(dict.fromkeys(subjects))
        with self.tasks.transaction(task_id, principal) as (db, task):
            if not principal.is_admin:
                raise TaskForbidden('只有 IT 管理员可以指定审批负责人员')
            for subject in subjects:
                profile = self.profile(db, task, subject)
                person = principal_from_record(profile)
                if subject == task['requester'] or person.employee_id == task['principal'].get('employee_id') or not person.has_any('approver', 'it_admin'):
                    raise TaskForbidden('审批人必须具备审批角色，且不能是申请人或申请人账号')
                if not self.writable(db, task, person):
                    raise TaskForbidden('请先将观察者或已移除人员重新邀请为协作者')
            required = db.execute(select(approvals.c.approvals_required).where(approvals.c.conv_id == 'task:' + task_id, approvals.c.status == 'pending')).scalars().all()
            if subjects and len(subjects) < max(required, default=1):
                raise TaskConflict('指定审批人数不能少于待审批申请要求')
            previous = set(db.execute(select(task_reviewers.c.subject).where(task_reviewers.c.task_id == task_id)).scalars())
            for subject in previous - set(subjects):
                role = db.execute(select(task_members.c.role).where(task_members.c.task_id == task_id, task_members.c.subject == subject)).scalar()
                person = principal_from_record(self.profile(db, task, subject))
                if role != 'collaborator' and not person.is_admin:
                    pending = db.execute(select(task_todos.c.id).where(task_todos.c.task_id == task_id,
                        task_todos.c.assignee == subject, task_todos.c.status == 'open')).first()
                    if pending:
                        raise TaskConflict('请先取消或改派该审批人的未完成待办，或将其邀请为协作者')
            db.execute(delete(task_reviewers).where(task_reviewers.c.task_id == task_id))
            for subject in set(subjects):
                db.execute(insert(task_reviewers).values(task_id=task_id, subject=subject))
            self.tasks.record(db, task, principal, 'reviewer_assignment', '更新了审批负责人员。', {'reviewers': subjects})

    def reviewers(self, task_id, principal):
        with self.tasks.read(task_id, principal) as (db, task):
            return list(db.execute(select(task_reviewers.c.subject).where(task_reviewers.c.task_id == task_id)).scalars())

    def approval_filter(self, principal):
        return approvals.c.conv_id.in_(select('task:' + service_tasks.c.id).where(self.filter(principal), approvals.c.tenant == service_tasks.c.tenant, approvals.c.requester == service_tasks.c.requester, approvals.c.department == service_tasks.c.department))

    def approval_access(self, db, row, principal, decision=False):
        task_id = row['conv_id'][5:]
        # Shared row lock serializes removal/assignment against votes.
        if decision:
            task = self.tasks._require(db, task_id, principal, lock=True)
        else:
            task = db.execute(select(service_tasks).where(service_tasks.c.id == task_id, self.filter(principal))).mappings().first()
        if not task or any(row[k] != task[k] for k in ('tenant', 'department', 'requester')):
            return False
        if not decision:
            return True
        assigned = list(db.execute(select(task_reviewers.c.subject).where(task_reviewers.c.task_id == task_id)).scalars())
        return self.writable(db, task, principal) and (not assigned or principal.subject in assigned)
