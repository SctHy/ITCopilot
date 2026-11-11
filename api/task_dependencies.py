"""Shared task transport dependencies; routes never call another route."""
import asyncio
from typing import Annotated
from fastapi import HTTPException
from pydantic import StringConstraints
from api import state
from workflow.tasks import TaskConflict, TaskForbidden

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8000)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


def wake_task_worker():
    if state.task_worker:
        state.task_worker.kick()


def service():
    if state.task_capabilities is None:
        raise HTTPException(503, 'task capabilities not ready')
    return state.task_capabilities


async def invoke(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except TaskForbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except TaskConflict as exc:
        raise HTTPException(409, str(exc)) from exc
