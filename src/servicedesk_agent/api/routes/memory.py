"""员工只管理自己的长期记忆；删除与审计共用数据库事务。"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException

from servicedesk_agent.api.container import Container, AppContainer
from servicedesk_agent.api.deps import current_principal
from servicedesk_agent.infra.identity import Principal

router = APIRouter()


def _ready(state: AppContainer):
    if state.memory is None:
        raise HTTPException(503, "memory unavailable")
    return state.memory


@router.get("/memory/me")
async def own_memory(state: Container, principal: Principal = Depends(current_principal)):
    return await _ready(state).personal_memories(principal.scope_key)


@router.delete("/memory/me")
async def delete_memory(state: Container, principal: Principal = Depends(current_principal)):
    count = await asyncio.to_thread(_ready(state).delete_personal_memories, principal)
    return {"deleted": count}


@router.delete("/memory/me/episodic/{episodic_id}")
async def delete_episodic(state: Container, episodic_id: str, principal: Principal = Depends(current_principal)):
    count = await asyncio.to_thread(_ready(state).delete_personal_memories, principal, episodic_id)
    if not count:
        raise HTTPException(404, "memory not found")
    return {"deleted": count}
