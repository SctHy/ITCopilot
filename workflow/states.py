"""Approval workflow lifecycles. ``WorkflowStore`` checks every status write against these tables."""
from __future__ import annotations

from enum import StrEnum

from core.states import Machine


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"      # replaced by a newer request in the same conversation
    INVALIDATED = "invalidated"    # the policy changed and no longer allows it


class Execution(StrEnum):
    """Derived from the approval's apply actions on every settle (``_refresh_execution``)."""
    NOT_EXECUTED = "not_executed"
    QUEUED = "queued"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"


class ActionStatus(StrEnum):
    QUEUED = "queued"
    EXECUTING = "executing"
    UNKNOWN = "unknown"            # ambiguous outcome: observe the target before applying again
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MANUAL_REVIEW = "manual_review"


class GrantStatus(StrEnum):
    ACTIVE = "active"
    PREEXISTING = "preexisting"    # the employee already had the level; nothing to revoke
    REVOKED = "revoked"
    REVOKE_FAILED = "revoke_failed"
    DRIFTED = "drifted"            # changed by someone else; automatic revocation stopped


A, E, S, G = ApprovalStatus, Execution, ActionStatus, GrantStatus

APPROVAL = Machine("approval.status", {
    A.PENDING: {A.APPROVED, A.REJECTED, A.EXPIRED, A.SUPERSEDED, A.INVALIDATED},
    A.APPROVED: set(), A.REJECTED: set(), A.EXPIRED: set(), A.SUPERSEDED: set(), A.INVALIDATED: set(),
})

EXECUTION = Machine("approval.execution", {
    E.NOT_EXECUTED: {E.QUEUED},
    E.QUEUED: {E.EXECUTING, E.FAILED},                         # failed: policy cancelled every action before it started
    E.EXECUTING: {E.SUCCEEDED, E.FAILED, E.MANUAL_REVIEW},
    E.MANUAL_REVIEW: {E.EXECUTING},                            # operator requeue
    E.SUCCEEDED: set(), E.FAILED: set(),
})

ACTION = Machine("action.status", {
    S.QUEUED: {S.EXECUTING, S.CANCELLED},
    S.UNKNOWN: {S.EXECUTING},
    S.EXECUTING: {S.SUCCEEDED, S.QUEUED, S.UNKNOWN, S.FAILED, S.MANUAL_REVIEW},
    S.MANUAL_REVIEW: {S.UNKNOWN},                              # operator requeue
    S.SUCCEEDED: set(), S.FAILED: set(), S.CANCELLED: set(),
})

GRANT = Machine("access_grants.status", {
    G.ACTIVE: {G.REVOKED, G.REVOKE_FAILED, G.DRIFTED},
    # The revoke action ended in manual_review; an operator requeue may still finish it.
    G.DRIFTED: {G.REVOKED, G.REVOKE_FAILED},
    G.REVOKE_FAILED: {G.REVOKED, G.DRIFTED},
    G.PREEXISTING: set(), G.REVOKED: set(),
})

# Actions nobody is working on any more without an operator.
SETTLED = frozenset({S.SUCCEEDED, S.FAILED, S.CANCELLED, S.MANUAL_REVIEW})
