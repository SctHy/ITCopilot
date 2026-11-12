"""Approval workflow lifecycles. ``WorkflowStore`` checks every status write against these machines."""
from __future__ import annotations

from enum import StrEnum

from core.states import Machine


class ApprovalStatus(StrEnum):
    DRAFT = "draft"                # drafted by the Agent; the requester has not confirmed it yet (ADR 0020)
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"      # replaced by a newer request in the same conversation
    INVALIDATED = "invalidated"    # the policy changed and no longer allows it
    WITHDRAWN = "withdrawn"        # the requester took it back before any approver decided


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


APPROVAL = Machine(ApprovalStatus, """
    draft   -> pending | expired | superseded | invalidated | withdrawn
    pending -> approved | rejected | expired | superseded | invalidated | withdrawn
""")

EXECUTION = Machine(Execution, """
    not_executed  -> queued
    queued        -> executing | failed           # failed: the policy cancelled every action before it started
    executing     -> succeeded | failed | manual_review
    manual_review -> executing                    # operator requeue
""")

ACTION = Machine(ActionStatus, """
    queued        -> executing | cancelled        # cancelled: policy change or saga compensation
    unknown       -> executing
    executing     -> succeeded | queued | unknown | failed | manual_review
    manual_review -> unknown                      # operator requeue: observe before applying again
""")

GRANT = Machine(GrantStatus, """
    active        -> revoked | revoke_failed | drifted
    drifted       -> revoked | revoke_failed      # the revoke action was requeued by an operator
    revoke_failed -> revoked | drifted
""")

# Not yet decided by anyone: a policy change re-checks these, time expires them.
OPEN = frozenset({ApprovalStatus.DRAFT, ApprovalStatus.PENDING})

# Actions nobody works on any more without an operator; the approval's execution then needs a notice.
SETTLED = frozenset({ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.CANCELLED, ActionStatus.MANUAL_REVIEW})
NOTIFIED = frozenset({Execution.SUCCEEDED, Execution.FAILED, Execution.MANUAL_REVIEW})
