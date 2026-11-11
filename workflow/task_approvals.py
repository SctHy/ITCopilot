"""Approval integration: transactional task effects and a durable event source.

Only this adapter knows both task and approval schemas. The task store/worker and
the Agent depend on ports; they do not poll or execute approval business logic.
"""
from sqlalchemy import String, and_, cast, select

from core import db as schema
from core.db import approvals, approval_decisions, service_tasks, task_events
from workflow.tasks import TaskForbidden, key
from core.identity import principal_from_record

STATUS = {"pending": "待审批", "approved": "已同意审批", "rejected": "已拒绝", "expired": "已过期",
          "invalidated": "策略变更已失效", "superseded": "已被新申请替代"}
EXECUTION = {"not_executed": "尚未执行", "queued": "排队中", "executing": "执行中", "succeeded": "执行成功",
             "failed": "执行失败", "manual_review": "需人工核对"}


class TaskApprovalEffects:
    def __init__(self, workflow):
        self.workflow = workflow

    def view(self, task):
        with schema.read(self.workflow.engine) as db:
            ids = db.execute(select(approvals.c.id).where(approvals.c.conv_id == "task:" + task["id"],
                approvals.c.requester == task["requester"], approvals.c.tenant == task["tenant"],
                approvals.c.department == task["department"])).scalars().all()
        items = [self.workflow.get(i) for i in ids]
        phase = ("awaiting_approval" if any(a["status"] == "pending" for a in items) else
                 "executing" if any(a["status"] == "approved" and a["execution"] in {"queued", "executing"} for a in items) else
                 "manual_review" if any(a["execution"] == "manual_review" for a in items) else "waiting")
        return {"approvals": items, "phase": phase}

    def commit(self, connection, task, event, claim, result):
        proposal = result.get("proposal")
        if not proposal:
            return {}
        if event["kind"] != "requester" or event["actor"] != task["requester"]:
            raise TaskForbidden("讨论与审批结果回合不能创建业务变更申请")
        approval_id = "task-" + claim["id"]
        self.workflow.create(approval_id, request_id=claim["id"], conv_id="task:" + task["id"],
            principal=principal_from_record(event["data"]["principal"]), action_requests=proposal["action_requests"],
            summary=proposal, connection=connection)
        return {"approval_id": approval_id}


class ApprovalEventSource:
    def __init__(self, workflow, task_store):
        self.workflow, self.tasks = workflow, task_store

    def poll(self):
        # Only rows not yet reflected in the task transcript are fetched, so a quiet system
        # costs two read queries per tick instead of one write transaction per historical row.
        task_join = and_(approvals.c.conv_id == "task:" + service_tasks.c.id, approvals.c.requester == service_tasks.c.requester,
            approvals.c.tenant == service_tasks.c.tenant, approvals.c.department == service_tasks.c.department)
        seen_status = select(task_events.c.id).where(task_events.c.task_id == service_tasks.c.id, task_events.c.kind == "approval",
            task_events.c.data["approval_id"].as_string() == approvals.c.id,
            task_events.c.data[("source_cursor", "version")].as_integer() >= approvals.c.version).exists()
        seen_vote = select(task_events.c.id).where(task_events.c.task_id == service_tasks.c.id, task_events.c.kind == "approval_vote",
            task_events.c.data[("source_cursor", "object_id")].as_string() == cast(approval_decisions.c.id, String)).exists()
        with schema.read(self.workflow.engine) as db:
            rows = db.execute(select(approvals, service_tasks.c.id.label("task_id")).join(service_tasks, task_join)
                .where(~and_(approvals.c.status == "pending", approvals.c.version == 0), ~seen_status)
                .order_by(approvals.c.created_at, approvals.c.id)).mappings().all()
            votes = db.execute(select(approval_decisions, service_tasks.c.id.label("task_id"), approvals.c.created_at.label("approval_created_at"))
                .join(approvals, approvals.c.id == approval_decisions.c.approval_id).join(service_tasks, task_join)
                .where(~seen_vote).order_by(approvals.c.created_at, approval_decisions.c.approval_id, approval_decisions.c.id)).mappings().all()
        pending_votes = {}
        for vote in votes:
            pending_votes.setdefault(vote["approval_id"], []).append(vote)
        updates = {row["id"]: row for row in rows}
        order = sorted({(r["created_at"], r["id"]) for r in rows} | {(v["approval_created_at"], v["approval_id"]) for v in votes})
        for _, approval_id in order:
            # Votes have their own immutable identities and are recorded before the status they
            # produced, so a delayed poll still shows both reviewers even if already executed.
            for decision in pending_votes.get(approval_id, []):
                label = "同意审批" if decision["decision"] == "approved" else "拒绝申请"
                self.tasks.ingest(decision["task_id"], source="approval_vote", object_id=str(decision["id"]), version=1,
                    kind="approval_vote", text=f"{decision['actor_name'] or decision['actor']}选择了“{label}”。",
                    actor=decision["actor"], actor_name=decision["actor_name"] or decision["actor"],
                    data={"approval_id": approval_id, "decision": decision["decision"]}, wake=False)
            row = updates.get(approval_id)
            if row is None:
                continue
            terminal = row["status"] in {"rejected", "expired", "invalidated"} or (
                row["status"] == "approved" and row["execution"] in {"succeeded", "failed", "manual_review"})
            self.tasks.ingest(row["task_id"], source="approval", object_id=row["id"], version=row["version"],
                event_id=key("approval", row["id"], row["version"]),
                kind="approval", actor_name="审批工作流",
                text=f"审批：{STATUS[row['status']]}；执行：{EXECUTION[row['execution']]}。",
                data={"approval_id": row["id"], "status": row["status"], "execution": row["execution"]}, wake=terminal)


class ApprovalDeadlineSource:
    """Remind once near expiry; decisions remain entirely in the approval service."""
    def __init__(self, tasks, *, lead_s=3600):
        self.tasks, self.lead_s = tasks, lead_s

    def poll(self):
        now = self.tasks.clock()
        seen = select(task_events.c.id).where(task_events.c.task_id == service_tasks.c.id,
            task_events.c.kind == 'approval_reminder', task_events.c.data['approval_id'].as_string() == approvals.c.id).exists()
        with schema.read(self.tasks.engine) as db:
            rows = db.execute(select(approvals.c.id, service_tasks.c.id.label('task_id')).join(service_tasks, and_(
                approvals.c.conv_id == 'task:' + service_tasks.c.id, approvals.c.requester == service_tasks.c.requester,
                approvals.c.tenant == service_tasks.c.tenant, approvals.c.department == service_tasks.c.department))
                .where(service_tasks.c.status == 'open', approvals.c.status == 'pending', approvals.c.expires_at > now,
                       approvals.c.expires_at <= now + self.lead_s, ~seen).order_by(approvals.c.expires_at).limit(100)).mappings().all()
        for row in rows:
            with self.tasks.transaction(row['task_id']) as (db, task):
                approval = db.execute(select(approvals).where(approvals.c.id == row['id']).with_for_update()).mappings().one()
                if task['status'] != 'open' or approval['status'] != 'pending' or approval['expires_at'] <= self.tasks.clock():
                    continue
                self.tasks.record(db, task, None, 'approval_reminder', '审批即将到期，请审批人查看申请。',
                    {'approval_id': row['id'], 'expires_at': approval['expires_at']}, event_id=key('approval-due', row['id']))
