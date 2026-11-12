"""Execution worker: drains the action outbox against target-system adapters.

Runs inside the API process by default and can run standalone
(``python -m workflow.worker``); several workers may share one database because every
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
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from workflow.adapters import AdapterRegistry
from workflow.adapters.base import AmbiguousError, PermanentError, TransientError, rank, target_level
from core import admin_audit
from core.change_log import append_change
from core.protocols import SERVICE
from core.concurrency import LeaseLostError, run_leased
from core.tenant_context import system_scope, tenant_scope
from monitor import metrics
from workflow.states import SETTLED, ActionStatus, ApprovalStatus, GrantStatus
from workflow.policy import load_policy, overlay_tenants, policy_signature
from workflow.store import LeaseLost, WorkflowStore
from workflow.lifecycle import EFFECT

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
        self.lifecycle = SERVICE.start()
        self._policy_version: Optional[str] = None  # version the last revalidation sweep ran against
        from workflow.reconciler import GrantReconciler
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
        self.lifecycle.send("start")
        self._task = asyncio.create_task(self._loop(), name="execution-worker")

    async def stop(self) -> None:
        if self.lifecycle.state == "stopped":
            return
        self.lifecycle.send("stop")
        self._wake.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self.lifecycle.send("stopped")

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
        # Expiry, revalidation and claiming span every tenant (system scope); each claimed action then
        # runs in its own tenant's scope, which the adapters and row-level security read (ADR 0023).
        with system_scope():
            return await self._run_once(limit)

    async def _run_once(self, limit: int) -> int:
        ages = await asyncio.to_thread(self.store.action_ages)
        metrics.WORKFLOW_OLDEST_AGE.labels(state="unknown").set(ages["oldest_unknown_action_age_s"])
        metrics.WORKFLOW_OLDEST_AGE.labels(state="queued_due").set(ages["oldest_queued_due_action_age_s"])
        await asyncio.to_thread(self.store.expire_pending)
        await self._revalidate_on_policy_change()
        now = self.store.clock()
        if self._last_reconcile is None or now - self._last_reconcile >= self.reconcile_interval_s:
            await self.reconciler.run_once()
            self._last_reconcile = now
        processed = 0
        seen = set()
        for _ in range(limit):
            claimed = await asyncio.to_thread(self.store.claim, self.worker_id, self.lease_s, 1, exclude_ids=seen)
            if not claimed:
                break
            seen.add(claimed[0]["id"])
            with tenant_scope(claimed[0]["tenant"]):
                await self._process_owned(claimed[0])
            processed += 1
        return processed

    async def _revalidate_on_policy_change(self) -> None:
        """Startup and every policy edit: re-check open approvals and grants (idempotent; several workers may race)."""
        # The base and every tenant overlay (ADR 0025): editing any of them re-checks the open work it governs.
        version = await asyncio.to_thread(lambda: self.store._policy.version if self.store._policy else policy_signature())
        if version == self._policy_version:
            return
        report = await asyncio.to_thread(self.store.revalidate)
        changed = {k: len(v) for k, v in report.items() if isinstance(v, list) and v}
        await asyncio.to_thread(self._record_policy_loaded, self.store.policy.version, changed)
        await asyncio.to_thread(self._record_overlays_loaded)
        self._policy_version = version
        for outcome, count in changed.items():
            metrics.POLICY_REVALIDATIONS.labels(outcome=outcome).inc(count)
        logger.info("access policy %s: revalidated open work %s", version, changed or "(no changes)")

    def _record_policy_loaded(self, version: str, changed: Dict[str, int]) -> None:
        policy = self.store.policy
        self._record_policy(admin_audit.GLOBAL_TENANT, version, lambda last: {
            "declared_version": policy.declared_version, "fingerprint": policy.fingerprint,
            "previous": last["subject"] if last is not None else None, "revalidated": changed, "worker": self.worker_id})

    def _record_overlays_loaded(self) -> None:
        if self.store._policy:
            return
        for tenant in overlay_tenants():
            policy = load_policy(tenant)
            self._record_policy(tenant, policy.version, lambda last: {
                "base": self.store.policy.version, "fingerprint": policy.fingerprint, "worker": self.worker_id})

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

    async def wait_settled(self, approval_id: str, timeout_s: float) -> Optional[Dict[str, Any]]:
        """Wait until every apply action of the approval is terminal (or the timeout passes)."""
        self.kick()
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            body = await asyncio.to_thread(self.store.get, approval_id)
            if body is None or body["status"] != ApprovalStatus.APPROVED or all(r["status"] in SETTLED for r in body["results"]):
                return body
            if asyncio.get_running_loop().time() >= deadline:
                return body
            await asyncio.sleep(0.05)

    async def wait_revoked(self, approval_id: str, timeout_s: float) -> Optional[Dict[str, Any]]:
        """Wait until no revocation of the approval is due and unfinished (or the timeout passes)."""
        self.kick()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while True:
            body = await asyncio.to_thread(self.store.get, approval_id)
            now = self.store.clock()
            due = [r for r in (body or {}).get("revocations", []) if r["status"] in {ActionStatus.EXECUTING, ActionStatus.UNKNOWN}
                   or (r["status"] == ActionStatus.QUEUED and r["not_before"] and datetime.fromisoformat(r["not_before"]).timestamp() <= now)]
            if body is None or not due or loop.time() >= deadline:
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

    async def _process_owned(self, action):
        try:
            await run_leased(lambda: self._process(action),
                             lambda: asyncio.to_thread(self.store.renew, action, self.lease_s), self.lease_s / 3)
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
                    revocation = await asyncio.to_thread(self.store.revocation_context, action)
                    saved = {"restore_to": target_level(adapter, revocation["restore_to"])}
                    await asyncio.to_thread(self.store.save_result, action, saved)
                spec = {"tool_name": "revoke_access", "arguments": {**action["arguments"], "restore_to": saved["restore_to"]}}
            if action["kind"] == "apply" and (action["prev_status"] == ActionStatus.QUEUED or (
                    action["tool_name"] == "request_access" and action["result"] is None)):
                flow.send("preflight")
                # The policy may have changed since approval. QUEUED means this action has made no
                # change yet, including a retry after a definite (transient) failure: the policy sweep
                # runs once per version and skips attempted actions, so every such attempt re-checks.
                reason = await asyncio.to_thread(self.store.policy_block, action["approval_id"])
                if reason:
                    raise PermanentError(reason)
                if action["seq"] == 0 and action["attempts"] == 1:
                    # Validate the whole batch before the first effect: most partial failures
                    # (unknown user, missing repo) are caught with zero changes applied.
                    for item in await asyncio.to_thread(self.store.apply_actions, action["approval_id"]):
                        await self.registry.by_name(item["adapter"]).preflight({"tool_name": item["tool_name"], "arguments": item["arguments"]})
            observed = False
            if action["prev_status"] in {ActionStatus.UNKNOWN, ActionStatus.EXECUTING} and (
                    action["tool_name"] != "request_access" or action["result"] is not None):
                flow.send("observe")
                observed = await adapter.observe(action["id"], spec)
            if observed:
                if action["tool_name"] == "revoke_access":
                    latest = await asyncio.to_thread(self.store.revocation_context, action)
                    restore = target_level(adapter, latest["restore_to"])
                    current = await adapter.current_level(spec["arguments"]["employee_id"], spec["arguments"]["system"])
                    if current != restore:
                        flow.send("baseline")
                        await asyncio.to_thread(self.store.fail, action,
                            f"旧回收目标 {spec['arguments']['restore_to']} 已生效，但当前权限 {current} 不等于最新台账目标 {restore}；不能复用幂等键修改目标，需要人工核查",
                            status=ActionStatus.MANUAL_REVIEW, ledger_status=GrantStatus.DRIFTED)
                        flow.send("drift")
                        await self._emit(action, "action_drifted", current=current, restore_to=restore)
                        return
                flow.send("reconciled")
                result = {**(action.get("result") or {}), "reconciled": True}
                preexisting = action["tool_name"] == "request_access" and rank(result["baseline_level"]) >= rank(adapter.level(spec["arguments"]["role"]))
                await asyncio.to_thread(self.store.succeed, action, result, preexisting=preexisting)
                flow.send("commit")
                await self._emit(action, "action_reconciled")
                return
            result = dict(action.get("result") or {})
            if action["tool_name"] == "request_access":
                flow.send("baseline")
                current = await adapter.current_level(spec["arguments"]["employee_id"], spec["arguments"]["system"])
                grant_context = await asyncio.to_thread(self.store.grant_context, action, current)
                if grant_context["covering"]:
                    expected = target_level(adapter, grant_context["expected_level"])
                    if current != expected:
                        await asyncio.to_thread(self.store.fail, action,
                            "当前权限与有效授权台账不一致，停止新授权并转人工核查", status=ActionStatus.MANUAL_REVIEW,
                            resource_drift={"current": current, "expected": expected})
                        flow.send("drift")
                        metrics.GRANT_DRIFT.labels(system=spec["arguments"]["system"]).inc()
                        await self._emit(action, "action_drifted", current=current, expected=expected)
                        return
                if "baseline_level" not in result:
                    result["baseline_level"] = grant_context["baseline_level"]
                    await asyncio.to_thread(self.store.save_result, action, result)
                if rank(result["baseline_level"]) >= rank(adapter.level(spec["arguments"]["role"])):
                    flow.send("preexisting")
                    await asyncio.to_thread(self.store.succeed, action, {**result, "preexisting": True}, preexisting=True)
                    flow.send("commit")
                    await self._emit(action, "action_succeeded", preexisting=True)
                    return
                covered = next((grant["action_id"] for grant in grant_context["covering"]
                                if rank(adapter.level(grant["role"])) >= rank(adapter.level(spec["arguments"]["role"]))
                                and rank(current) >= rank(adapter.level(spec["arguments"]["role"]))), None)
                if covered is not None:
                    flow.send("preexisting")
                    await asyncio.to_thread(self.store.succeed, action, {**result, "covered_by": covered})
                    flow.send("commit")
                    await self._emit(action, "action_succeeded", covered_by=covered)
                    return
            elif action["tool_name"] == "revoke_access":
                flow.send("baseline")
                revocation = await asyncio.to_thread(self.store.revocation_context, action)
                current = await adapter.current_level(spec["arguments"]["employee_id"], spec["arguments"]["system"])
                if current != target_level(adapter, revocation["expected"]):
                    await asyncio.to_thread(self.store.fail, action, f"权限已被他人修改（当前 {current}），停止自动回收", status=ActionStatus.MANUAL_REVIEW, ledger_status=GrantStatus.DRIFTED)
                    flow.send("drift")
                    await self._emit(action, "action_drifted", current=current)
                    return
                restore = target_level(adapter, revocation["restore_to"])
                if result["restore_to"] != restore:
                    result["restore_to"] = restore
                    await asyncio.to_thread(self.store.save_result, action, result)
                spec = {"tool_name": "revoke_access", "arguments": {**action["arguments"], "restore_to": restore}}
            flow.send("apply")
            result.update(await adapter.apply(action["id"], spec))
            flow.send("verify")
            if not await adapter.observe(action["id"], spec):
                raise AmbiguousError("变更后回查未看到目标状态")
            flow.send("verified")
            await asyncio.to_thread(self.store.succeed, action, result)
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
            await self._settle(self.store.fail, action, str(exc))
            await self._emit(action, "action_failed", error=str(exc))
        except TransientError as exc:
            flow.send("fail")
            if action["attempts"] >= self.max_attempts:
                await self._settle(self.store.fail, action, f"重试 {action['attempts']} 次仍失败：{exc}")
                await self._emit(action, "action_failed", error=str(exc))
            else:
                await self._settle(self.store.retry_later, action, ActionStatus.QUEUED, str(exc), self._delay(action["attempts"]))
                await self._emit(action, "action_retry", error=str(exc))
        except Exception as exc:  # AmbiguousError and anything unexpected: outcome unknown
            if flow.state in flow.protocol.transitions:
                flow.send("fail")
            if not isinstance(exc, AmbiguousError):
                logger.exception("unexpected adapter failure for action %s", action["id"])
            if action["attempts"] >= self.max_attempts:
                await self._settle(self.store.fail, action, f"结果持续未知，需人工核对：{exc}", status=ActionStatus.MANUAL_REVIEW)
                await self._emit(action, "action_manual_review", error=str(exc))
            else:
                await self._settle(self.store.retry_later, action, ActionStatus.UNKNOWN, str(exc), self._delay(action["attempts"]))
                await self._emit(action, "action_unknown", error=str(exc))

    @staticmethod
    async def _settle(fn, *args, **kwargs) -> None:
        try:
            await asyncio.to_thread(fn, *args, **kwargs)
        except LeaseLost:
            logger.warning("lease lost while settling action %s", args[0]["id"])


def build_registry(store: WorkflowStore) -> AdapterRegistry:
    """Local sandbox for everything; Gitea for the systems listed in IT_COPILOT_GITEA_REPOS, and per
    tenant for the tenants in IT_COPILOT_TENANT_ADAPTERS (ADR 0025)."""
    from core.env import multi_tenant_enabled
    from workflow.adapters.local import LocalDirectoryAdapter

    external = {}
    repos = dict(item.split("=", 1) for item in os.getenv("IT_COPILOT_GITEA_REPOS", "").split(",") if "=" in item)
    if repos and os.getenv("IT_COPILOT_GITEA_URL"):
        basic = (os.environ["IT_COPILOT_GITEA_USER"], os.getenv("IT_COPILOT_GITEA_PASSWORD", "")) if os.getenv("IT_COPILOT_GITEA_USER") else None
        gitea = _gitea(store, os.environ["IT_COPILOT_GITEA_URL"], repos, os.getenv("IT_COPILOT_GITEA_TOKEN", ""), basic)
        external = {system: gitea for system in repos}
    tenants = {tenant: {system: adapter for system, adapter in adapters.items()}
               for tenant, adapters in load_tenant_adapters(store).items()}
    # Multi-tenant: the deployment's own credentials act for the operator tenant only.
    shared = {os.getenv("IT_COPILOT_PLATFORM_TENANT", "default")} if multi_tenant_enabled() else None
    return AdapterRegistry(LocalDirectoryAdapter(store.engine), external, tenants=tenants, shared_with=shared)


def _gitea(store, url, repos, token, basic):
    from workflow.adapters.gitea import GiteaAdapter

    return GiteaAdapter(url, token, repos, store.username_of, basic_auth=basic, timeout_s=float(os.getenv("IT_COPILOT_GITEA_TIMEOUT_S", "10")))


TENANT_ADAPTER_KEYS = {"url", "repos", "token_env", "user_env", "password_env"}


def load_tenant_adapters(store, path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """``config/tenant_adapters.yaml``: each tenant's own target systems. Credentials are named by
    environment variable (``token_env`` ...), never written in the file; a missing variable or an
    inline secret refuses to start rather than running a tenant's grants with no or the wrong account.

        tenants:
          acme:
            gitea: {url: https://git.acme.example, repos: {gitlab: acme/platform}, token_env: ACME_GITEA_TOKEN}
    """
    import pathlib

    import yaml

    path = path or os.getenv("IT_COPILOT_TENANT_ADAPTERS", str(pathlib.Path(__file__).resolve().parent.parent / "config/tenant_adapters.yaml"))
    if not pathlib.Path(path).is_file():
        return {}
    data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    result: Dict[str, Dict[str, Any]] = {}
    for tenant, systems in (data.get("tenants") or {}).items():
        for kind, conf in (systems or {}).items():
            if kind != "gitea":
                raise ValueError(f"tenant {tenant}: unsupported adapter {kind!r}")
            unknown = set(conf) - TENANT_ADAPTER_KEYS
            if unknown:
                raise ValueError(f"tenant {tenant}: unsupported keys {sorted(unknown)} (credentials go in environment variables: token_env, user_env, password_env)")
            secrets = {k: os.getenv(conf[k], "") for k in ("token_env", "user_env", "password_env") if conf.get(k)}
            missing = [conf[k] for k in secrets if not secrets[k] and k != "password_env"]
            if missing or not secrets:
                raise ValueError(f"tenant {tenant}: credentials not set ({missing or 'token_env or user_env required'})")
            basic = (secrets["user_env"], secrets.get("password_env", "")) if "user_env" in secrets else None
            adapter = _gitea(store, conf["url"], dict(conf["repos"]), secrets.get("token_env", ""), basic)
            result.setdefault(tenant, {}).update({system: adapter for system in conf["repos"]})
    return result


def enabled_channels() -> set:
    return {"feishu"} if os.getenv("FEISHU_APP_ID") else set()


def database_url(default_dir: Optional[str] = None) -> str:
    url = os.getenv("IT_COPILOT_DATABASE_URL")
    if url:
        return url
    base = default_dir or os.getenv("IT_COPILOT_DATA_DIR", "data")
    return f"sqlite:///{os.path.abspath(os.path.join(base, 'it_copilot.sqlite3'))}"


async def _main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    from core import tenant_context
    from core.tenancy import ensure_supported

    tenant_context.enter(tenant_context.SYSTEM)  # the standalone worker spans tenants; run_once scopes each action
    ensure_supported(database_url())
    store = WorkflowStore(database_url())
    ensure_supported(store.url, store.engine)
    channels = enabled_channels()
    if channels:
        # Approvals this process settles still notify the IM conversation that asked for them.
        from intake.im import approval_notifier
        store.notify = approval_notifier(channels)
    registry = build_registry(store)
    store.route = lambda action: registry.for_action(action).name
    worker = ExecutionWorker(store, registry, poll_s=float(os.getenv("IT_COPILOT_WORKER_POLL_S", "1")))
    from workflow.tasks import TaskStore
    worker.reconciler.tasks = TaskStore(store.engine, clock=store.clock)
    import redis.asyncio as redis
    from workflow.badcase_miner import BadcaseMiner, RedisTurnSnapshots
    snapshots = redis.from_url(os.getenv("REDIS_URL", "redis://:it-copilot123@127.0.0.1:6379/0"), decode_responses=True)
    miner = BadcaseMiner(store.engine, RedisTurnSnapshots(snapshots))
    miner.start()
    worker.start()
    logger.info("execution worker %s started", worker.worker_id)
    try:
        await asyncio.Event().wait()
    finally:
        await miner.stop()
        await snapshots.aclose()
        await worker.stop()


if __name__ == "__main__":
    asyncio.run(_main())
