"""Execution worker: drains the action outbox against target-system adapters.

Runs inside the API process by default and can run standalone
(``python -m workflow.worker``); several workers may share one database because every
claim and transition is a version CAS guarded by a lease.

Per action:

1. Claimed with a lease. If the previous status was ``unknown`` or an expired
   ``executing`` lease (crash mid-call), observe the target first and only re-apply
   when the effect is absent.
2. Grants record the holder's baseline level *before* the first change; an existing
   equal-or-higher permission is left untouched and never revoked later.
3. After apply, verify with an independent read. Verification failure is treated
   as an ambiguous outcome, not success.
4. Successful grants schedule their revocation at expiry in the same transaction.
   Revocation checks for drift: if someone changed the permission since we granted
   it, the worker stops and asks for review instead of deleting their change.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import socket
import uuid
from typing import Any, Callable, Dict, Optional

from adapters import AdapterRegistry
from adapters.base import AmbiguousError, PermanentError, TransientError, rank
from monitor import metrics
from workflow.store import TERMINAL, LeaseLost, WorkflowStore

logger = logging.getLogger(__name__)

EventSink = Callable[[str, str, Dict[str, Any]], Any]


class ExecutionWorker:
    def __init__(self, store: WorkflowStore, registry: AdapterRegistry, *, worker_id: Optional[str] = None,
                 lease_s: float = 60.0, poll_s: float = 1.0, max_attempts: int = 5, base_delay_s: float = 2.0,
                 events: Optional[EventSink] = None):
        self.store = store
        self.registry = registry
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self.lease_s = lease_s
        self.poll_s = poll_s
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.events = events
        self._wake = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stopping = False
        self._task = asyncio.create_task(self._loop(), name="execution-worker")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    def kick(self) -> None:
        self._wake.set()

    async def _loop(self) -> None:
        while not self._stopping:
            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("execution worker iteration failed")
                processed = 0
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_s)
            except TimeoutError:
                pass

    async def run_once(self, limit: int = 10) -> int:
        claimed = await asyncio.to_thread(self.store.claim, self.worker_id, self.lease_s, limit)
        for action in claimed:
            await self._process(action)
        return len(claimed)

    async def run_until_idle(self, max_rounds: int = 100) -> None:
        for _ in range(max_rounds):
            if not await self.run_once():
                return

    async def wait_settled(self, approval_id: str, timeout_s: float) -> Optional[Dict[str, Any]]:
        """Wait until every apply action of the approval is terminal (or the timeout passes)."""
        self.kick()
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            body = await asyncio.to_thread(self.store.get, approval_id)
            if body is None or body["status"] != "approved" or all(r["status"] in TERMINAL for r in body["results"]):
                return body
            if asyncio.get_running_loop().time() >= deadline:
                return body
            await asyncio.sleep(0.05)

    # ── processing ──────────────────────────────────────────────────────────

    def _delay(self, attempts: int) -> float:
        return self.base_delay_s * (2 ** (attempts - 1)) * (0.5 + random.random())

    async def _emit(self, action, event: str, **data) -> None:
        metrics.WORKFLOW_ACTIONS.labels(kind=action["kind"], outcome=event).inc()
        if self.events:
            try:
                result = self.events("approval:" + action["approval_id"], event, {"action_id": action["id"], "kind": action["kind"], "tool": action["tool_name"], **data})
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.warning("workflow event publish failed", exc_info=True)

    async def _process(self, action: Dict[str, Any]) -> None:
        adapter = self.registry.by_name(action["adapter"])
        spec = {"tool_name": action["tool_name"], "arguments": action["arguments"]}
        try:
            await self._emit(action, "action_started", attempt=action["attempts"])
            if action["kind"] == "apply" and action["seq"] == 0 and action["prev_status"] == "queued" and action["attempts"] == 1:
                # Validate the whole batch before the first effect: most partial failures
                # (unknown user, missing repo) are caught with zero changes applied.
                for item in await asyncio.to_thread(self.store.apply_actions, action["approval_id"]):
                    await self.registry.by_name(item["adapter"]).preflight({"tool_name": item["tool_name"], "arguments": item["arguments"]})
            if action["prev_status"] in {"unknown", "executing"} and await adapter.observe(action["id"], spec):
                result = {**(action.get("result") or {}), "reconciled": True}
                await asyncio.to_thread(self.store.succeed, action, result)
                await self._emit(action, "action_reconciled")
                return
            result = dict(action.get("result") or {})
            if action["tool_name"] == "request_access":
                if "baseline_level" not in result:
                    result["baseline_level"] = await adapter.current_level(spec["arguments"]["employee_id"], spec["arguments"]["system"])
                    await asyncio.to_thread(self.store.save_result, action, result)
                if rank(result["baseline_level"]) >= rank(adapter.level(spec["arguments"]["role"])):
                    await asyncio.to_thread(self.store.succeed, action, {**result, "preexisting": True}, preexisting=True)
                    await self._emit(action, "action_succeeded", preexisting=True)
                    return
            elif action["tool_name"] == "revoke_access":
                current = await adapter.current_level(spec["arguments"]["employee_id"], spec["arguments"]["system"])
                if current != adapter.level(spec["arguments"]["role"]):
                    await asyncio.to_thread(self.store.fail, action, f"权限已被他人修改（当前 {current}），停止自动回收", status="manual_review", ledger_status="drifted")
                    await self._emit(action, "action_drifted", current=current)
                    return
            result.update(await adapter.apply(action["id"], spec))
            if not await adapter.observe(action["id"], spec):
                raise AmbiguousError("变更后回查未看到目标状态")
            await asyncio.to_thread(self.store.succeed, action, result)
            await self._emit(action, "action_succeeded")
        except LeaseLost:
            logger.warning("lease lost for action %s; another worker owns it", action["id"])
        except PermanentError as exc:
            await self._settle(self.store.fail, action, str(exc))
            await self._emit(action, "action_failed", error=str(exc))
        except TransientError as exc:
            if action["attempts"] >= self.max_attempts:
                await self._settle(self.store.fail, action, f"重试 {action['attempts']} 次仍失败：{exc}")
                await self._emit(action, "action_failed", error=str(exc))
            else:
                await self._settle(self.store.retry_later, action, "queued", str(exc), self._delay(action["attempts"]))
                await self._emit(action, "action_retry", error=str(exc))
        except Exception as exc:  # AmbiguousError and anything unexpected: outcome unknown
            if not isinstance(exc, AmbiguousError):
                logger.exception("unexpected adapter failure for action %s", action["id"])
            if action["attempts"] >= self.max_attempts:
                await self._settle(self.store.fail, action, f"结果持续未知，需人工核对：{exc}", status="manual_review")
                await self._emit(action, "action_manual_review", error=str(exc))
            else:
                await self._settle(self.store.retry_later, action, "unknown", str(exc), self._delay(action["attempts"]))
                await self._emit(action, "action_unknown", error=str(exc))

    @staticmethod
    async def _settle(fn, *args, **kwargs) -> None:
        try:
            await asyncio.to_thread(fn, *args, **kwargs)
        except LeaseLost:
            logger.warning("lease lost while settling action %s", args[0]["id"])


def build_registry(store: WorkflowStore) -> AdapterRegistry:
    """Local sandbox for everything; Gitea for the systems listed in IT_COPILOT_GITEA_REPOS."""
    from adapters.local import LocalDirectoryAdapter

    external = {}
    repos = dict(item.split("=", 1) for item in os.getenv("IT_COPILOT_GITEA_REPOS", "").split(",") if "=" in item)
    if repos and os.getenv("IT_COPILOT_GITEA_URL"):
        from adapters.gitea import GiteaAdapter

        basic = (os.environ["IT_COPILOT_GITEA_USER"], os.getenv("IT_COPILOT_GITEA_PASSWORD", "")) if os.getenv("IT_COPILOT_GITEA_USER") else None
        gitea = GiteaAdapter(os.environ["IT_COPILOT_GITEA_URL"], os.getenv("IT_COPILOT_GITEA_TOKEN", ""), repos, store.username_of,
                             basic_auth=basic, timeout_s=float(os.getenv("IT_COPILOT_GITEA_TIMEOUT_S", "10")))
        external = {system: gitea for system in repos}
    return AdapterRegistry(LocalDirectoryAdapter(store.engine), external)


def database_url(default_dir: Optional[str] = None) -> str:
    url = os.getenv("IT_COPILOT_DATABASE_URL")
    if url:
        return url
    base = default_dir or os.getenv("CHROMA_PERSIST_DIRECTORY", "data/chroma")
    return f"sqlite:///{os.path.abspath(os.path.join(base, 'workflow.sqlite3'))}"


async def _main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    store = WorkflowStore(database_url())
    registry = build_registry(store)
    store.route = lambda action: registry.for_action(action).name
    worker = ExecutionWorker(store, registry, poll_s=float(os.getenv("IT_COPILOT_WORKER_POLL_S", "1")))
    worker.start()
    logger.info("execution worker %s started", worker.worker_id)
    try:
        await asyncio.Event().wait()
    finally:
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(_main())
