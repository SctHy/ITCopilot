"""Approval use case shared by private inbox and collaborative task transports."""
import asyncio

from monitor import metrics
from workflow.store import ApprovalForbidden


class ApprovalService:
    def __init__(self, store, worker=None, *, wait_s=5):
        self.store, self.worker, self.wait_s = store, worker, wait_s

    async def decide(self, approval_id, decision, payload_hash, principal):
        if not principal.has_any("approver", "it_admin"):
            raise ApprovalForbidden("当前身份没有审批权限")
        before = await asyncio.to_thread(self.store.get, approval_id)
        body = await asyncio.to_thread(self.store.decide, approval_id, decision, principal, payload_hash)
        if not body:
            raise LookupError("approval not found")
        if before and before["status"] == "pending" and body["status"] != "pending":
            metrics.APPROVALS.labels(event=body["status"]).inc()
        if body["status"] == "approved" and self.worker:
            body = await self.worker.wait_settled(approval_id, self.wait_s) or body
        return body
