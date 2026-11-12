"""Durable rollout state with short read caches and stable identity bucketing."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
from enum import StrEnum

from sqlalchemy import insert, select, update
from sqlalchemy.dialects import postgresql, sqlite

from core.db import admin_audit, agent_rollouts, change_events, feedback, read, turn_records
from core.states import Machine
from core.tenant_context import system_scope
from evaluation.attribution import wilson
from evaluation.reporting import percentile


class RolloutStatus(StrEnum):
    STABLE = "stable"
    CANARYING = "canarying"
    ROLLED_BACK = "rolled_back"
    PROMOTED = "promoted"


ROLLOUT = Machine(RolloutStatus, """
    stable -> canarying | rolled_back
    canarying -> canarying | promoted | rolled_back
    rolled_back -> canarying | rolled_back
    promoted -> canarying | rolled_back
""")


def canary_bucket(name, scope_key):
    return int(hashlib.sha256((name + scope_key).encode()).hexdigest(), 16) % 100


class AgentRollouts:
    def __init__(self, engine, registry, *, cache_ttl_s=5., clock=time.time,
                 reports=Path(__file__).resolve().parents[1] / "evaluation/reports/release"):
        self.engine, self.registry, self.clock = engine, registry, clock
        self.cache_ttl_s, self.reports = cache_ttl_s, Path(reports)
        self._cache = {}
        with system_scope(), engine.begin() as db:
            statement = (postgresql if db.dialect.name == "postgresql" else sqlite).insert(agent_rollouts)
            for name in registry.names:
                db.execute(statement.values(spec_name=name, stable_version=registry.initial_version(name),
                    canary_version=None, canary_percent=0, canary_tenants=None, status="stable",
                    updated_at=clock(), updated_by="system").on_conflict_do_nothing())
            for row in db.execute(select(agent_rollouts)).mappings():
                registry.get(row["spec_name"], row["stable_version"])
                if row["canary_version"] is not None:
                    registry.get(row["spec_name"], row["canary_version"])

    def get(self, name):
        now = time.monotonic()
        cached = self._cache.get(name)
        if cached is not None and now - cached[0] < self.cache_ttl_s:
            return dict(cached[1])
        with system_scope(), read(self.engine) as db:
            record = dict(db.execute(select(agent_rollouts).where(agent_rollouts.c.spec_name == name)).mappings().one())
        self._cache[name] = now, record
        return dict(record)

    def choose(self, tenant, intent_group, scope_key, state):
        if "agent_version" in state:
            return self.registry.get(state["agent_spec"], state["agent_version"])
        name = self.registry.resolve(tenant, intent_group)
        rollout = self.get(name)
        eligible = rollout["canary_tenants"] is None or tenant in rollout["canary_tenants"]
        canary = eligible and canary_bucket(name, scope_key) < rollout["canary_percent"]
        version = rollout["canary_version"] if canary else rollout["stable_version"]
        state.update(agent_spec=name, agent_version=version)
        return self.registry.get(name, version)

    def change(self, name, operation, actor, *, tenant, version=None, percent=None, tenants=None, expected=None):
        if operation == "canary":
            self.registry.get(name, version)
            report = json.loads((self.reports / f"{name}@{version}.json").read_text())
            if report["spec"] != name or report["version"] != version or report["status"] != "passed":
                raise ValueError("Release evaluation did not pass for this specification")
            if report["fingerprint"] != self.registry.get(name, version).fingerprint:
                raise ValueError("Release report does not match the specification fingerprint")
        with system_scope(), self.engine.begin() as db:
            # Lock the row before reading state on both supported databases.
            db.execute(update(agent_rollouts).where(agent_rollouts.c.spec_name == name).values(updated_at=agent_rollouts.c.updated_at))
            old = dict(db.execute(select(agent_rollouts).where(agent_rollouts.c.spec_name == name)).mappings().one())
            if expected is not None and any(old[field] != expected[field] for field in (
                    "stable_version", "canary_version", "status", "updated_at")):
                self._cache.pop(name, None)
                return None
            if operation == "canary":
                values = dict(canary_version=version, canary_percent=percent, canary_tenants=tenants, status="canarying")
            elif operation == "promote":
                if old["status"] != "canarying":
                    raise ValueError("No canary version to promote")
                values = dict(stable_version=old["canary_version"], canary_version=None, canary_percent=0,
                              canary_tenants=None, status="promoted")
            elif operation == "rollback":
                values = dict(canary_percent=0, status="rolled_back")
            else:
                raise ValueError(f"Unknown rollout operation: {operation}")
            ROLLOUT.check(old["status"], values["status"])
            values.update(updated_at=self.clock(), updated_by=actor)
            db.execute(update(agent_rollouts).where(agent_rollouts.c.spec_name == name).values(**values))
            detail = {"operation": operation, "before": old, "after": {**old, **values}, "actor_tenant": tenant}
            db.execute(insert(change_events).values(at=self.clock(), tenant=None, kind="agent_rollout",
                       ref=name, detail=detail, actor=actor))
            db.execute(insert(admin_audit).values(tenant="*", actor=actor,
                event="agent_rollout", subject=name, detail=detail, at=self.clock(), outcome="succeeded"))
        self._cache.pop(name, None)
        return self.get(name)

    def auto_rollback(self, *, feedback_rows=None):
        from agents.outcome import turn_outcome
        rolled_back = []
        with system_scope(), read(self.engine) as db:
            rows = [dict(row) for row in db.execute(select(turn_records).where(
                turn_records.c.created_at >= self.clock() - 1800)).mappings()]
            if feedback_rows is None:
                feedback_rows = db.execute(select(feedback).join(turn_records,
                    feedback.c.request_id == turn_records.c.request_id).where(
                    turn_records.c.created_at >= self.clock() - 1800)).mappings().all()
        negative = {row["request_id"] for row in feedback_rows if row["rating"] == -1}
        for name in self.registry.names:
            rollout = self.get(name)
            if rollout["status"] != "canarying":
                continue
            stable = [r for r in rows if r["agent_spec"] == name and r["agent_version"] == rollout["stable_version"]]
            canary = [r for r in rows if r["agent_spec"] == name and r["agent_version"] == rollout["canary_version"]]
            if len(canary) < 100 or not stable:
                continue
            fail0 = sum(turn_outcome(row) != "success" for row in stable)
            fail1 = sum(turn_outcome(row) != "success" for row in canary)
            # Newcombe's difference interval combines the two Wilson score intervals.
            p0, p1 = fail0 / len(stable), fail1 / len(canary)
            w0, w1 = wilson(fail0, len(stable)), wilson(fail1, len(canary))
            difference_lower = p1 - p0 - ((p1 - w1[0]) ** 2 + (w0[1] - p0) ** 2) ** .5
            claims = lambda group: sum("claim_noted" in r["signals"] for r in group) / len(group)
            bad_feedback = lambda group: sum(r["request_id"] in negative for r in group) / len(group)
            reasons = []
            if difference_lower > 0:
                reasons.append("failure_rate")
            if claims(canary) > 2 * claims(stable):
                reasons.append("claim_noted")
            if bad_feedback(canary) > 2 * bad_feedback(stable):
                reasons.append("negative_feedback")
            if percentile([r["latency_ms"] for r in canary], .95) > 1.5 * percentile([r["latency_ms"] for r in stable], .95):
                reasons.append("latency")
            if reasons:
                changed = self.change(name, "rollback", "system", tenant=None, expected=rollout)
                if changed is not None:
                    rolled_back.append({"name": name, "reasons": reasons})
        return rolled_back
