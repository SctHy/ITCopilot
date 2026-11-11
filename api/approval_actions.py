"""HTTP composition and error translation for the shared approval service."""
from fastapi import HTTPException

from api import state
from api.deps import require_workflow
from core.env import env_float
from monitor import metrics
from workflow.approval_service import ApprovalService
from workflow.store import ApprovalForbidden


async def submit_decision(approval_id, payload, principal):
    service = ApprovalService(require_workflow(), state.worker, wait_s=env_float("IT_COPILOT_APPROVAL_WAIT_S", 5))
    try:
        body = await service.decide(approval_id, payload.decision, payload.payload_hash, principal)
    except ApprovalForbidden as exc:
        metrics.AUTH_FAILURES.labels(reason="separation_of_duties").inc()
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    if state.task_event_pump:
        state.task_event_pump.kick()
    return body
