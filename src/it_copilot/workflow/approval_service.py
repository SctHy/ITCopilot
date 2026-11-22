"""Approval use case shared by private inbox and collaborative task transports."""

import asyncio
from datetime import datetime
from typing import Any, Dict, Optional

from it_copilot.workflow.states import SETTLED, ActionStatus, ApprovalStatus
from it_copilot.infra.monitor import metrics
from it_copilot.workflow.store import ApprovalForbidden


class ApprovalService:
    def __init__(self, store, *, wait_s=5):
        self.store, self.wait_s = store, wait_s

    async def decide(self, approval_id, decision, payload_hash, principal):
        if not principal.has_any("approver", "it_admin"):
            raise ApprovalForbidden("当前身份没有审批权限")
        before = await self.store.run(self.store.get, approval_id)
        body = await self.store.run(self.store.decide, approval_id, decision, principal, payload_hash)
        if not body:
            raise LookupError("approval not found")
        if before and before["status"] == "pending" and body["status"] != "pending":
            metrics.APPROVALS.labels(event=body["status"]).inc()
        if body["status"] == "approved" and self.wait_s > 0:
            body = await wait_settled(self.store, approval_id, self.wait_s) or body
        return body


async def wait_settled(store, approval_id: str, timeout_s: float) -> Optional[Dict[str, Any]]:
    """Wait until every apply action of the approval is terminal (or the timeout passes)."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while True:
        body = await store.run(store.get, approval_id)
        if body is None or body["status"] != ApprovalStatus.APPROVED or all(r["status"] in SETTLED for r in body["results"]):
            return body
        if asyncio.get_running_loop().time() >= deadline:
            return body
        await asyncio.sleep(0.05)


async def wait_revoked(store, approval_id: str, timeout_s: float) -> Optional[Dict[str, Any]]:
    """Wait until no revocation of the approval is due and unfinished (or the timeout passes)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        body = await store.run(store.get, approval_id)
        now = store.clock()
        due = [r for r in (body or {}).get("revocations", []) if r["status"] in {ActionStatus.EXECUTING, ActionStatus.UNKNOWN}
               or (r["status"] == ActionStatus.QUEUED and r["not_before"] and datetime.fromisoformat(r["not_before"]).timestamp() <= now)]
        if body is None or not due or loop.time() >= deadline:
            return body
        await asyncio.sleep(0.05)
