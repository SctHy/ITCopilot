"""Shared transient protocols. State is owned by the operation using start(), not here."""
from enum import StrEnum

from servicedesk_agent.infra.states import Protocol


SERVICE = Protocol("service", "stopped", {
    "stopped": {"start": "running", "stopped": "stopped", "close": "closed"},
    "running": {"stop": "stopping"},
    "stopping": {"stop": "stopping", "stopped": "stopped", "close": "closed"},
    "closed": {"close": "closed"},
})

APPLICATION = Protocol("application", "starting", {
    "starting": {"ready": "ready", "degrade": "degraded", "stop": "stopping"},
    "ready": {"stop": "stopping"}, "degraded": {"stop": "stopping"},
    "stopping": {"close": "closed"},
})

SNAPSHOT = Protocol("snapshot", "empty", {
    "empty": {"load": "loading"},
    "loading": {"publish": "ready", "partial": "degraded", "fail": "failed"},
    "ready": {"load": "loading"}, "degraded": {"load": "loading"}, "failed": {"load": "loading"},
})

MIGRATION = Protocol("embedding_migration", "scanning", {
    "scanning": {"batch": "embedding", "finish": "completed", "fail": "failed"},
    "embedding": {"write": "writing", "fail": "failed"},
    "writing": {"commit": "scanning", "fail": "failed"},
})

LEASE = Protocol("lease", "waiting", {
    "waiting": {"acquired": "held", "timeout": "unavailable", "cancel": "released"},
    "held": {"renew": "held", "lost": "lost", "release": "released"},
    "lost": {"release": "released"},
})

JOB = Protocol("coalesced_job", "idle", {
    "idle": {"submit": "queued"},
    "queued": {"submit": "queued", "take": "running", "cancel": "idle"},
    "running": {"submit": "dirty", "finish": "idle", "cancel": "idle"},
    "dirty": {"submit": "dirty", "take": "running", "cancel": "idle"},
})


class CredentialState(StrEnum):
    MISSING = "missing"
    VALID = "valid"
    EXPIRED = "expired"


def credential_state(token, expires_at, now):
    if not token:
        return CredentialState.MISSING
    return CredentialState.VALID if now < expires_at else CredentialState.EXPIRED
