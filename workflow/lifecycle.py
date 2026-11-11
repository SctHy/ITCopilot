"""Derived cross-row states: never persist a second, potentially divergent status."""
from enum import StrEnum
from core.states import Protocol
from workflow.states import ActionStatus as A, ApprovalStatus as P, Execution as E


class WorkPhase(StrEnum):
    WAITING = "waiting"
    READY = "ready"
    BLOCKED = "blocked"
    LEASED = "leased"
    RECLAIMABLE = "reclaimable"
    REVIEW = "review"
    SETTLED = "settled"


def work_phase(row, now, *, blocked=False):
    if row["status"] == A.MANUAL_REVIEW:
        return WorkPhase.REVIEW
    if row["status"] == A.EXECUTING:
        return WorkPhase.RECLAIMABLE if (row.get("lease_until") or 0) < now else WorkPhase.LEASED
    if row["status"] not in {A.QUEUED, A.UNKNOWN}:
        return WorkPhase.SETTLED
    if blocked:
        return WorkPhase.BLOCKED
    return WorkPhase.READY if row["not_before"] <= now else WorkPhase.WAITING


def approval_phase(row, decisions, rows, now):
    if row["status"] == P.PENDING:
        if row["expires_at"] <= now:
            return "expiry_due"
        return "awaiting_quorum" if decisions else "awaiting_first_vote"
    if row["status"] != P.APPROVED:
        return str(row["status"])
    revokes = [a for a in rows if a["kind"] == "revoke"]
    if row["execution"] == E.MANUAL_REVIEW or any(a["status"] in {A.MANUAL_REVIEW, A.FAILED} for a in revokes):
        return "manual_review"
    open_revokes = [a for a in revokes if a["status"] in {A.QUEUED, A.EXECUTING, A.UNKNOWN}]
    if row["execution"] == E.FAILED:
        return "compensating" if open_revokes else "failed"
    if row["execution"] != E.SUCCEEDED:
        return str(row["execution"])
    if any(a["status"] != A.QUEUED or a["not_before"] <= now for a in open_revokes):
        return "revoking"
    return "active_until_revocation" if open_revokes else "completed"


_EFFECT_STAGES = {
    "claimed": {"preflight": "preflight", "observe": "observing", "baseline": "baseline", "apply": "applying", "fail": "failed"},
    "preflight": {"baseline": "baseline", "apply": "applying", "fail": "failed"},
    "observing": {"reconciled": "verified", "baseline": "baseline", "apply": "applying", "fail": "failed"},
    "baseline": {"preexisting": "verified", "apply": "applying", "drift": "review", "fail": "failed"},
    "applying": {"verify": "verifying", "fail": "failed"},
    "verifying": {"verified": "verified", "fail": "failed"},
    "verified": {"commit": "committed", "fail": "failed"},
}

# Every in-flight stage can stop before its durable outcome is settled.
EFFECT = Protocol("external_effect", "claimed", {
    state: {**events, "fail": "failed", "cancel": "cancelled", "lost": "lease_lost"}
    for state, events in _EFFECT_STAGES.items()
})
