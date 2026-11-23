"""Real outbox workloads. Only the baseline arm replaces dispatch or recovery policy."""

import asyncio
from collections import Counter
from time import monotonic, perf_counter_ns, time

from sqlalchemy import select

from servicedesk_agent.cmd.bench import MemoryTarget, approve
from servicedesk_agent.infra.db import access_grants, actions, read
from servicedesk_agent.infra.identity import Principal
from servicedesk_agent.infra.tenant_context import current_tenant, tenant_scope
from servicedesk_agent.workflow.adapters import AdapterRegistry
from servicedesk_agent.workflow.worker import ExecutionWorker
from .stats import jains_fairness_index, percentile


def apply_rows(store):
    with read(store.engine) as db:
        return db.execute(select(actions).where(actions.c.kind == "apply")).mappings().all()


async def notify(store, protected, profile, poll=None):
    poll = poll or (1.0 if profile == "quick" else 5.0)
    target = MemoryTarget()
    starts = {}

    def event(stream, kind, data):
        if kind == "action_started":
            starts[data["action_id"]] = time()

    worker = ExecutionWorker(
        store, AdapterRegistry(target), events=event, poll_s=5 if protected else poll
    )
    empty = 0
    original = store.actions.claim

    async def counted(*args, **kwargs):
        nonlocal empty
        rows = await original(*args, **kwargs)
        empty += int(not rows)
        return rows

    store.actions.claim = counted
    if not protected:
        worker.notifier.start = lambda: None
    begin = monotonic()
    worker.start()
    try:
        async with asyncio.timeout(30):
            while not empty or (
                protected and "postgresql" in store.url and not worker.notifier.source
            ):
                await asyncio.sleep(0.005)
            # Identical idle interval exposes fallback polling cost before a burst.
            idle_start = monotonic()
            idle_before = empty
            await asyncio.sleep(poll * 2.2)
            idle_rate = (empty - idle_before) / (monotonic() - idle_start)
            for i in range(6):
                await store.run(approve, store, f"notify-{i}", i)
                await asyncio.sleep(0.025)
            while len(target.levels) < 6:
                await asyncio.sleep(0.01)
            while sum(r["status"] == "succeeded" for r in apply_rows(store)) < 6:
                await asyncio.sleep(0.01)
        rows = apply_rows(store)
        latency = [1000 * (starts[r["id"]] - r["not_before"]) for r in rows]
        checks = dict(
            missing_tasks=6 - len(rows),
            incomplete_tasks=sum(r["status"] != "succeeded" for r in rows),
            duplicate_writes=sum(target.writes.values()) - len(target.writes),
        )
        if protected and "postgresql" in store.url:
            checks["missing_pg_notify"] = int(worker.notifier.source != "notify")
        return {
            **{f"p{p}_ms": percentile(latency, p) for p in (50, 95, 99)},
            "idle_empty_claims_per_s": idle_rate,
            "empty_claims_per_s": empty / (monotonic() - begin),
        }, checks
    finally:
        await worker.stop()


def submit(store, tenant, i):
    employee = str((10000 if tenant == "noisy" else 20000) + i)
    name = f"{tenant}-{i}"
    owner = Principal(name, name, "engineering", frozenset({"employee"}), employee, tenant)
    reviewer = Principal("review", "review", "engineering", frozenset({"approver"}), "2001", tenant)
    with tenant_scope(tenant):
        row = store.create(
            name,
            request_id=name,
            conv_id=name,
            principal=owner,
            action_requests=[
                dict(
                    tool_name="request_access",
                    arguments=dict(
                        employee_id=employee, system="gitlab", role="readonly", reason="eval"
                    ),
                )
            ],
        )
        store.decide(name, "approved", reviewer, row["payload_hash"])


async def fair(store, protected, workers, backlog=160, quiet=8):
    class Target(MemoryTarget):
        async def apply(self, action_id, action):
            owner[action_id] = current_tenant()
            return await super().apply(action_id, action)

    target = Target()
    owner = {}
    if not protected:
        tx = store.actions.transactions
        lock = tx.lock_next
        tx.lock_next = lambda due, excluded, after: lock(due, excluded, None)

        def oldest(db, due, excluded, limit):
            rows = db.execute(
                select(actions)
                .where(due, actions.c.id.not_in(excluded))
                .order_by(actions.c.not_before, actions.c.seq, actions.c.id)
                .limit(limit)
            ).mappings()
            return [dict(row, tenant_rank=i) for i, row in enumerate(rows)]

        tx._candidates = oldest
    for i in range(backlog):
        submit(store, "noisy", i)
    completion = {}

    def event(stream, kind, data):
        if kind == "action_succeeded":
            completion[data["action_id"]] = time()

    pool = [ExecutionWorker(store, AdapterRegistry(target), events=event) for _ in range(workers)]
    begin = time()

    async def drain(w):
        while await w.run_once():
            pass

    async def feed():
        for i in range(quiet):
            await store.run(submit, store, "quiet", i)
            await asyncio.sleep(0.01)

    await asyncio.gather(feed(), *(drain(w) for w in pool))
    rows = apply_rows(store)
    b = [r for r in rows if r["tenant"] == "quiet"]
    window = sorted(completion, key=completion.get)[: 2 * quiet]
    counts = Counter(owner[k] for k in window)
    errors = sum(
        r["status"] != "succeeded" or target.levels.get(r["arguments"]["employee_id"]) != "readonly"
        for r in rows
    )
    return dict(
        b_p95_ms=percentile([(completion[r["id"]] - r["not_before"]) * 1000 for r in b], 95),
        b_window_completion=counts["quiet"] / quiet,
        jain=jains_fairness_index([counts["noisy"], counts["quiet"]]),
        a_window_per_s=counts["noisy"] / (completion[window[-1]] - begin),
    ), dict(
        inconsistent=errors + abs(len(rows) - backlog - quiet),
        cross_tenant=sum(owner.get(r["id"]) != r["tenant"] for r in rows),
        duplicate_writes=sum(target.writes.values()) - len(target.writes),
    )


SCENES = (
    "before_apply",
    "ambiguous_after_apply",
    "stale_lease_split_brain",
    "external_drift_reconcile",
)


async def fault(store, protected, cases):
    observed_ns = []

    class Target(MemoryTarget):
        async def observe(self, *args):
            start = perf_counter_ns()
            result = await super().observe(*args)
            observed_ns.append(perf_counter_ns() - start)
            return result

    target = Target()
    worker = ExecutionWorker(store, AdapterRegistry(target))
    offset = 0
    store.clock = lambda: time() + offset
    totals = Counter()
    metrics = {}
    for scene in SCENES:
        errors = duplicates = detected = 0
        for i in range(cases):
            resource = SCENES.index(scene) * cases + i
            name = f"fault-{resource}"
            approve(store, name, resource)
            (old,) = await store.actions.claim("interrupted", 1, 1)
            aid = old["id"]
            if scene == "external_drift_reconcile":
                # Both arms start with a normally settled grant before the manual change.
                with tenant_scope(old["tenant"]):
                    await worker._process_owned(old)
            else:
                if scene != "stale_lease_split_brain":
                    await store.actions.save_result(old, {"baseline_level": None})
                if scene == "ambiguous_after_apply":
                    await target.apply(aid, old)
                offset += 2
                if protected:
                    # Resume the old worker after takeover, before the winner writes.
                    # Its first durable pre-write checkpoint still carries the old epoch.
                    async def resume_old(stream, event, detail):
                        if event == "action_started":
                            with tenant_scope(old["tenant"]):
                                await ExecutionWorker(store, worker.registry)._process_owned(old)

                    worker.events = resume_old if scene == "stale_lease_split_brain" else None
                    await worker.run_once(limit=1)
                    worker.events = None
                else:
                    (new,) = await store.actions.claim("blind", 60, 1)
                    if scene == "stale_lease_split_brain":
                        await asyncio.gather(target.apply(aid, old), target.apply(aid, new))
                    else:
                        await target.apply(aid, new)
                    await store.actions.succeed(new, {"baseline_level": None})
            employee = old["arguments"]["employee_id"]
            if scene == "external_drift_reconcile":
                target.levels[employee] = "developer"
                if protected:
                    await worker.reconciler.run_once()
            with read(store.engine) as db:
                status = db.scalar(
                    select(access_grants.c.status).where(access_grants.c.action_id == aid)
                )
                action_status = db.scalar(select(actions.c.status).where(actions.c.id == aid))
            drift = scene == "external_drift_reconcile"
            detected += int(drift and status == "drifted")
            consistent = (
                status == ("drifted" if drift else "active")
                and action_status == "succeeded"
                and target.levels.get(employee) == ("developer" if drift else "readonly")
            )
            errors += int(not consistent)
            duplicates += max(0, target.writes[aid] - 1)
        metrics[f"{scene}/duplicate_writes_per_case"] = duplicates / cases
        metrics[f"{scene}/consistency"] = 1 - errors / cases
        totals.update(errors=errors, duplicates=duplicates, detected=detected)
    n = cases * len(SCENES)
    metrics.update(
        duplicate_external_write_rate=sum(v > 1 for v in target.writes.values()) / n,
        drift_detection_rate=totals["detected"] / cases,
        final_consistency=1 - totals["errors"] / n,
        observe_ms_per_case=sum(observed_ns) / 1e6 / n,
    )
    return metrics, dict(
        duplicate_writes=totals["duplicates"],
        inconsistent=totals["errors"],
        missed_drift=cases - totals["detected"],
    )
