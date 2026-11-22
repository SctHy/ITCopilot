"""Execution worker: drains the action outbox against target-system adapters.

Start with ``python -m it_copilot.cmd.worker``. Several workers may share one database because every
claim and transition is a version CAS guarded by a lease.

Per action:

1. Claimed with a lease. If the previous status was ``unknown`` or an expired
   ``executing`` lease (crash mid-call), observe the target first and only re-apply
   when the effect is absent.
2. Grants inherit the resource's original baseline from its active ledger. Live state
   must still match that ledger; external changes require review before a new grant.
3. After apply, verify with an independent read. Verification failure is treated
   as an ambiguous outcome, not success.
4. Successful grants schedule their revocation at expiry in the same transaction.
   Before an approval's first effect, and whenever the policy version changes, open
   work is re-checked against the current policy (``WorkflowStore.revalidate``).
   Revocation compares the live state with the complete active ledger, then restores
   its remaining desired state. Unknown commands reserve the resource until reconciled.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import socket
import time
import uuid
from typing import Any, Callable, Dict, Optional

from it_copilot.workflow.adapters import AdapterRegistry
from it_copilot.workflow.adapters.base import AmbiguousError, PermanentError, TransientError, rank, target_level
from it_copilot.infra.monitor.telemetry import trace_scope
from it_copilot.infra.logging import action_fields
from it_copilot.infra import admin_audit
from it_copilot.infra.change_log import append_change
from it_copilot.infra.protocols import SERVICE
from it_copilot.infra.concurrency import LeaseLostError, run_leased
from it_copilot.infra.tenant_context import system_scope, tenant_scope
from it_copilot.infra.monitor import metrics
from it_copilot.workflow.states import ActionStatus, GrantStatus
from it_copilot.workflow.store import LeaseLost, WorkflowStore
from it_copilot.workflow.lifecycle import EFFECT
from it_copilot.workflow.notifier import ActionNotifier

logger = logging.getLogger(__name__)

EventSink = Callable[[str, str, Dict[str, Any]], Any]


class ExecutionWorker:
    def __init__(self, store: WorkflowStore, registry: AdapterRegistry, *, worker_id: Optional[str] = None,
                 lease_s: float = 60.0, poll_s: float = 5.0, max_attempts: int = 5, base_delay_s: float = 2.0,
                 events: Optional[EventSink] = None):
        self.store = store
        self._last_tenant: Optional[str] = None  # tenant served last; the next claim starts after it
        self.registry = registry
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self.lease_s = lease_s
        self.poll_s = poll_s
        self.max_attempts = max_attempts
        self.base_delay_s = base_delay_s
        self.events = events
        self.notifier = ActionNotifier(store.url)
        self._wake = self.notifier.event
        self.draining = False
        self._task: Optional[asyncio.Task] = None
        self.lifecycle = SERVICE.start()
        self._policy_version: Optional[str] = None  # version the last revalidation sweep ran against
        from it_copilot.workflow.reconciler import GrantReconciler
        self.reconcile_interval_s = float(os.environ.get("IT_COPILOT_RECONCILE_INTERVAL_S", "900"))
        if not math.isfinite(self.reconcile_interval_s) or self.reconcile_interval_s <= 0:
            raise ValueError("IT_COPILOT_RECONCILE_INTERVAL_S must be a positive finite number")
        self.reconciler = GrantReconciler(store, registry)
        self._last_reconcile = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    @property
    def _stopping(self):
        return self.lifecycle.state != "running"

    def start(self) -> None:
        if self.lifecycle.state == "running":
            return
        self.draining = False
        self.notifier.start()
        self.lifecycle.send("start")
        self._task = asyncio.create_task(self._loop(), name="execution-worker")

    async def stop(self) -> None:
        if self.lifecycle.state == "stopped":
            return
        self.draining = True
        self.lifecycle.send("stop")
        await self.notifier.close()
        self._wake.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, float(os.getenv("IT_COPILOT_SHUTDOWN_GRACE_S", "15")))
            except TimeoutError:
                pass
        self._task = None
        self.lifecycle.send("stopped")

    def kick(self) -> None:
        self.notifier.signal('kick')

    async def _loop(self) -> None:
        while not self._stopping:
            self._wake.clear()
            try:
                processed = await self.run_once()
            except Exception:
                logger.exception("execution worker iteration failed")
                processed = 0
            if processed:
                continue
            await self.notifier.wait(self.poll_s)

    async def run_once(self, limit: int = 10) -> int:
        # Expiry, revalidation and claiming span every tenant (system scope); each claimed action then
        # runs in its own tenant's scope, which the adapters and row-level security read (ADR 0023).
        with system_scope():
            return await self._run_once(limit)

    async def _run_once(self, limit: int) -> int:
        ages = await self.store.actions.action_ages()
        metrics.WORKFLOW_OLDEST_AGE.labels(state="unknown").set(ages["oldest_unknown_action_age_s"])
        metrics.WORKFLOW_OLDEST_AGE.labels(state="queued_due").set(ages["oldest_queued_due_action_age_s"])
        await self.store.run(self.store.expire_pending)
        await self._revalidate_on_policy_change()
        now = self.store.clock()
        if self._last_reconcile is None or now - self._last_reconcile >= self.reconcile_interval_s:
            await self.reconciler.run_once()
            self._last_reconcile = now
        processed = 0
        seen = set()
        for _ in range(limit):
            if self.draining:
                break
            claim = asyncio.create_task(self.store.actions.claim(self.worker_id, self.lease_s, 1,
                exclude_ids=seen, after_tenant=self._last_tenant))
            try:
                claimed = await asyncio.shield(claim)
            except asyncio.CancelledError:
                for action in await claim:
                    await self.store.actions.release_lease(action, uncertain=False)
                raise
            if not claimed:
                break
            seen.add(claimed[0]["id"])
            self._last_tenant = claimed[0]["tenant"]
            with tenant_scope(claimed[0]["tenant"]), trace_scope(claimed[0]["traceparent"], "workflow.execute"), action_fields(claimed[0]):
                if self.draining:
                    await self.store.actions.release_lease(claimed[0], uncertain=False)
                    break
                try:
                    await self._process_owned(claimed[0])
                except asyncio.CancelledError:
                    try:
                        await self.store.actions.release_lease(claimed[0], uncertain=claimed[0].get("effect_started", False))
                    except LeaseLost:
                        pass
                    raise
                except Exception:
                    try:
                        await self.store.actions.release_lease(claimed[0], reason="error")
                    except LeaseLost:
                        pass
                    raise
            processed += 1
        return processed

    async def _revalidate_on_policy_change(self) -> None:
        """Startup and every policy edit: re-check open approvals and grants (idempotent; several workers may race)."""
        version = self.store.policy.version
        if version == self._policy_version:
            return
        report = await self.store.run(self.store.revalidate)
        changed = {k: len(v) for k, v in report.items() if isinstance(v, list) and v}
        await self.store.run(self._record_policy_loaded, self.store.policy.version, changed)
        self._policy_version = version
        for outcome, count in changed.items():
            metrics.POLICY_REVALIDATIONS.labels(outcome=outcome).inc(count)
        logger.info("access policy %s: revalidated open work %s", version, changed or "(no changes)")

    def _record_policy_loaded(self, version: str, changed: Dict[str, int]) -> None:
        policy = self.store.policy
        self._record_policy(admin_audit.GLOBAL_TENANT, version, lambda last: {
            "declared_version": policy.declared_version, "fingerprint": policy.fingerprint,
            "previous": last["subject"] if last is not None else None, "revalidated": changed, "worker": self.worker_id})

    def _record_policy(self, tenant: str, version: str, detail: Callable[[Optional[Dict[str, Any]]], Dict[str, Any]]) -> None:
        """The audit marker and its change event commit together; only the first sighting of a version is recorded."""
        is_global = tenant == admin_audit.GLOBAL_TENANT
        with (system_scope() if is_global else tenant_scope(tenant)), self.store.engine.begin() as db:
            last = admin_audit.last_event(db, tenant, "policy_loaded")
            if last is not None and last["subject"] == version:
                return
            now, body = time.time(), detail(last)
            admin_audit.append(db, tenant=tenant, actor=admin_audit.SYSTEM_ACTOR, event="policy_loaded",
                               subject=version, detail=body, at=now)
            append_change(db, "policy", version, body, tenant=None if is_global else tenant, at=now)

    async def run_until_idle(self, max_rounds: int = 100) -> None:
        for _ in range(max_rounds):
            if not await self.run_once():
                return

    # ── processing ──────────────────────────────────────────────────────────

    def _delay(self, attempts: int) -> float:
        return self.base_delay_s * (2 ** (attempts - 1)) * (0.5 + random.random())

    async def _emit(self, action, event: str, **data) -> None:
        logger.info("workflow %s", event)
        metrics.WORKFLOW_ACTIONS.labels(kind=action["kind"], outcome=event).inc()
        if self.events:
            try:
                result = self.events("approval:" + action["approval_id"], event, {"action_id": action["id"], "kind": action["kind"], "tool": action["tool_name"], **data})
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.warning("workflow event publish failed", exc_info=True)

    async def _process_owned(self, action):
        try:
            await run_leased(lambda: self._process(action),
                             lambda: self.store.actions.renew(action, self.lease_s), self.lease_s / 3)
        except LeaseLostError:
            logger.warning("action lease lost for %s", action["id"])

    async def _process(self, action: Dict[str, Any]) -> None:
        flow = EFFECT.start()
        adapter = self.registry.by_name(action["adapter"])
        spec = {"tool_name": action["tool_name"], "arguments": action["arguments"]}
        try:
            await self._emit(action, "action_started", attempt=action["attempts"])
            if action["tool_name"] == "revoke_access":
                saved = action["result"]
                if saved is None:
                    revocation = await self.store.run(self.store.revocation_context, action)
                    saved = {"restore_to": target_level(adapter, revocation["restore_to"])}
                    await self.store.actions.save_result(action, saved)
                spec = {"tool_name": "revoke_access", "arguments": {**action["arguments"], "restore_to": saved["restore_to"]}}
            if action["kind"] == "apply" and (action["prev_status"] == ActionStatus.QUEUED or (
                    action["tool_name"] == "request_access" and action["result"] is None)):
                flow.send("preflight")
                # The policy may have changed since approval. QUEUED means this action has made no
                # change yet, including a retry after a definite (transient) failure: the policy sweep
                # runs once per version and skips attempted actions, so every such attempt re-checks.
                reason = await self.store.run(self.store.policy_block, action["approval_id"])
                if reason:
                    raise PermanentError(reason)
                if action["seq"] == 0:
                    # Validate the whole batch before the first effect: most partial failures
                    # (unknown user, missing repo) are caught with zero changes applied.
                    for item in await self.store.actions.apply_actions(action["approval_id"]):
                        await self.registry.by_name(item["adapter"]).preflight({"tool_name": item["tool_name"], "arguments": item["arguments"]})
            observed = False
            if action["prev_status"] in {ActionStatus.UNKNOWN, ActionStatus.EXECUTING}:
                flow.send("observe")
                observed = await adapter.observe(action["id"], spec)
            if observed:
                if action["tool_name"] == "request_access" and "baseline_level" not in (action["result"] or {}):
                    flow.send("fail")
                    await self.store.actions.fail(action, "外部效果已存在但缺少授权前基线", status=ActionStatus.MANUAL_REVIEW)
                    await self._emit(action, "action_manual_review")
                    return
                if action["tool_name"] == "revoke_access":
                    latest = await self.store.run(self.store.revocation_context, action)
                    restore = target_level(adapter, latest["restore_to"])
                    current = await adapter.current_level(spec["arguments"]["employee_id"], spec["arguments"]["system"])
                    if current != restore:
                        flow.send("baseline")
                        await self.store.actions.fail(action,
                            f"旧回收目标 {spec['arguments']['restore_to']} 已生效，但当前权限 {current} 不等于最新台账目标 {restore}；不能复用幂等键修改目标，需要人工核查",
                            status=ActionStatus.MANUAL_REVIEW, ledger_status=GrantStatus.DRIFTED)
                        flow.send("drift")
                        await self._emit(action, "action_drifted", current=current, restore_to=restore)
                        return
                flow.send("reconciled")
                result = {**(action.get("result") or {}), "reconciled": True}
                preexisting = action["tool_name"] == "request_access" and rank(result["baseline_level"]) >= rank(adapter.level(spec["arguments"]["role"]))
                await self.store.actions.succeed(action, result, preexisting=preexisting)
                flow.send("commit")
                await self._emit(action, "action_reconciled")
                return
            result = dict(action.get("result") or {})
            if action["tool_name"] == "request_access":
                flow.send("baseline")
                current = await adapter.current_level(spec["arguments"]["employee_id"], spec["arguments"]["system"])
                grant_context = await self.store.run(self.store.grant_context, action, current)
                if grant_context["covering"]:
                    expected = target_level(adapter, grant_context["expected_level"])
                    if current != expected:
                        await self.store.actions.fail(action,
                            "当前权限与有效授权台账不一致，停止新授权并转人工核查", status=ActionStatus.MANUAL_REVIEW,
                            resource_drift={"current": current, "expected": expected})
                        flow.send("drift")
                        metrics.GRANT_DRIFT.labels(system=spec["arguments"]["system"]).inc()
                        await self._emit(action, "action_drifted", current=current, expected=expected)
                        return
                if "baseline_level" not in result:
                    result["baseline_level"] = grant_context["baseline_level"]
                    await self.store.actions.save_result(action, result)
                if rank(result["baseline_level"]) >= rank(adapter.level(spec["arguments"]["role"])):
                    flow.send("preexisting")
                    await self.store.actions.succeed(action, {**result, "preexisting": True}, preexisting=True)
                    flow.send("commit")
                    await self._emit(action, "action_succeeded", preexisting=True)
                    return
                covered = next((grant["action_id"] for grant in grant_context["covering"]
                                if rank(adapter.level(grant["role"])) >= rank(adapter.level(spec["arguments"]["role"]))
                                and rank(current) >= rank(adapter.level(spec["arguments"]["role"]))), None)
                if covered is not None:
                    flow.send("preexisting")
                    await self.store.actions.succeed(action, {**result, "covered_by": covered})
                    flow.send("commit")
                    await self._emit(action, "action_succeeded", covered_by=covered)
                    return
            elif action["tool_name"] == "revoke_access":
                flow.send("baseline")
                revocation = await self.store.run(self.store.revocation_context, action)
                current = await adapter.current_level(spec["arguments"]["employee_id"], spec["arguments"]["system"])
                if current != target_level(adapter, revocation["expected"]):
                    await self.store.actions.fail(action, f"权限已被他人修改（当前 {current}），停止自动回收", status=ActionStatus.MANUAL_REVIEW, ledger_status=GrantStatus.DRIFTED)
                    flow.send("drift")
                    await self._emit(action, "action_drifted", current=current)
                    return
                restore = target_level(adapter, revocation["restore_to"])
                if result["restore_to"] != restore:
                    result["restore_to"] = restore
                    await self.store.actions.save_result(action, result)
                spec = {"tool_name": "revoke_access", "arguments": {**action["arguments"], "restore_to": restore}}
            flow.send("apply")
            action["effect_started"] = True
            result.update(await adapter.apply(action["id"], spec))
            flow.send("verify")
            if not await adapter.observe(action["id"], spec):
                raise AmbiguousError("变更后回查未看到目标状态")
            flow.send("verified")
            await self.store.actions.succeed(action, result)
            flow.send("commit")
            await self._emit(action, "action_succeeded")
        except asyncio.CancelledError:
            if flow.state in flow.protocol.transitions:
                flow.send("cancel")
            raise
        except LeaseLost:
            flow.send("lost")
            logger.warning("lease lost for action %s; another worker owns it", action["id"])
        except PermanentError as exc:
            flow.send("fail")
            await self._settle(self.store.actions.fail, action, str(exc))
            await self._emit(action, "action_failed", error=str(exc))
        except TransientError as exc:
            flow.send("fail")
            if action["attempts"] >= self.max_attempts:
                await self._settle(self.store.actions.fail, action, f"重试 {action['attempts']} 次仍失败：{exc}")
                await self._emit(action, "action_failed", error=str(exc))
            else:
                status = ActionStatus.UNKNOWN if action["prev_status"] in {ActionStatus.UNKNOWN, ActionStatus.EXECUTING} else ActionStatus.QUEUED
                await self._settle(self.store.actions.retry_later, action, status, str(exc), self._delay(action["attempts"]))
                await self._emit(action, "action_retry", error=str(exc))
        except Exception as exc:  # AmbiguousError and anything unexpected: outcome unknown
            if flow.state in flow.protocol.transitions:
                flow.send("fail")
            if not isinstance(exc, AmbiguousError):
                logger.error("unexpected adapter failure (%s)", type(exc).__name__)
            if action["attempts"] >= self.max_attempts:
                await self._settle(self.store.actions.fail, action, f"结果持续未知，需人工核对：{exc}", status=ActionStatus.MANUAL_REVIEW)
                await self._emit(action, "action_manual_review", error=str(exc))
            else:
                await self._settle(self.store.actions.retry_later, action, ActionStatus.UNKNOWN, str(exc), self._delay(action["attempts"]))
                await self._emit(action, "action_unknown", error=str(exc))

    async def _settle(self, fn, *args, **kwargs) -> None:
        try:
            await fn(*args, **kwargs)
        except LeaseLost:
            logger.warning("lease lost while settling action %s", args[0]["id"])


def build_registry(store: WorkflowStore) -> AdapterRegistry:
    """Local sandbox for everything; Gitea for the systems listed in IT_COPILOT_GITEA_REPOS."""
    from it_copilot.workflow.adapters.local import LocalDirectoryAdapter

    external = {}
    repos = dict(item.split("=", 1) for item in os.getenv("IT_COPILOT_GITEA_REPOS", "").split(",") if "=" in item)
    if repos and os.getenv("IT_COPILOT_GITEA_URL"):
        from it_copilot.workflow.adapters.gitea import GiteaAdapter

        basic = (os.environ["IT_COPILOT_GITEA_USER"], os.getenv("IT_COPILOT_GITEA_PASSWORD", "")) if os.getenv("IT_COPILOT_GITEA_USER") else None
        gitea = GiteaAdapter(os.environ["IT_COPILOT_GITEA_URL"], os.getenv("IT_COPILOT_GITEA_TOKEN", ""), repos, lambda employee: store.run(store.username_of, employee),
                             basic_auth=basic, timeout_s=float(os.getenv("IT_COPILOT_GITEA_TIMEOUT_S", "10")))
        external = {system: gitea for system in repos}
    # The deployment's own credentials act for the operator tenant only (ADR 0025).
    shared = {os.getenv("IT_COPILOT_PLATFORM_TENANT", "default")}
    return AdapterRegistry(LocalDirectoryAdapter(store.directory), external, shared_with=shared)


def database_url(default_dir: Optional[str] = None) -> str:
    url = os.getenv("IT_COPILOT_DATABASE_URL")
    if url:
        return url
    base = default_dir or os.getenv("IT_COPILOT_DATA_DIR", "data")
    return f"sqlite:///{os.path.abspath(os.path.join(base, 'it_copilot.sqlite3'))}"


def configured_store(url):
    store = WorkflowStore(url, route=lambda action: registry.for_action(action).name)
    try:
        registry = build_registry(store)
    except Exception:
        store.close()
        raise
    return store, registry
