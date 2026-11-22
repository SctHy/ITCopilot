"""本人的网页私聊历史：列出、打开、改名、删除。只按调用者的 (tenant, scope key) 读写，管理员也读不到他人的私聊。"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from it_copilot.api.container import Container, AppContainer
from it_copilot.api.deps import current_principal
from it_copilot.infra import conversation_history as history
from it_copilot.infra.db import read
from it_copilot.infra.identity import Principal

router = APIRouter()


class Rename(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


def _store(state: AppContainer):
    if state.workflow is None:
        raise HTTPException(503, "service not ready")
    return state.workflow


@router.get("/conversations")
async def list_conversations(state: Container, principal: Principal = Depends(current_principal), q: str = Query("", max_length=200),
                             limit: int = Query(30, ge=1, le=100), before: Optional[float] = None):
    store = _store(state)

    def run():
        with read(store.engine) as db:
            return history.list_conversations(db, principal, q=q, limit=limit, before=before)
    return await store.run(run)


@router.get("/conversations/{conv_id}")
async def get_conversation(state: Container, conv_id: str, principal: Principal = Depends(current_principal)):
    store = _store(state)

    def run():
        with read(store.engine) as db:
            return history.get_conversation(db, principal, conv_id)
    found = await store.run(run)
    if found is None:
        raise HTTPException(404, "conversation not found")
    return found


@router.patch("/conversations/{conv_id}")
async def rename_conversation(state: Container, conv_id: str, body: Rename, principal: Principal = Depends(current_principal)):
    store = _store(state)

    def run():
        with store.engine.begin() as db:
            return history.rename_conversation(db, principal, conv_id, body.title)
    try:
        renamed = await store.run(run)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if not renamed:
        raise HTTPException(404, "conversation not found")
    return {"conv_id": conv_id, "title": body.title}


@router.delete("/conversations/{conv_id}")
async def delete_conversation(state: Container, conv_id: str, principal: Principal = Depends(current_principal)):
    store = _store(state)

    def run():
        with store.engine.begin() as db:
            return history.delete_conversation(db, principal, conv_id)
    if await store.run(run) is None:
        # Also the answer for someone else's conversation: existence is not disclosed.
        raise HTTPException(404, "conversation not found")
    return {"deleted": True}
