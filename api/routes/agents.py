"""Platform Agent versions and release-gated rollouts."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from api import state
from api.deps import require_platform_admin
from core.identity import Principal

router = APIRouter()


def admin(principal: Principal = Depends(require_platform_admin)):
    return principal


class Canary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    percent: int = Field(ge=0, le=100)
    tenants: list[str] | None = None


@router.get("/admin/agents")
def list_agents(principal: Principal = Depends(admin)):
    registry = state.agent_rollouts.registry
    return [{"name": name, "rollout": state.agent_rollouts.get(name), "versions": [
        {"version": spec.version, "fingerprint": spec.fingerprint,
         "serves": spec.serves.model_dump(mode="json"), "tenants": sorted(spec.tenants)}
        for spec in registry.specs.values() if spec.name == name]} for name in registry.names]


def change(name, operation, principal, **kwargs):
    try:
        return state.agent_rollouts.change(name, operation, principal.subject, tenant=principal.tenant, **kwargs)
    except KeyError:
        raise HTTPException(404, "Agent 规格或版本不存在")
    except FileNotFoundError:
        raise HTTPException(409, "缺少通过的发布评测报告")
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@router.post("/admin/agents/{name}/canary")
def canary(name: str, body: Canary, principal: Principal = Depends(admin)):
    return change(name, "canary", principal, version=body.version, percent=body.percent, tenants=body.tenants)


@router.post("/admin/agents/{name}/promote")
def promote(name: str, principal: Principal = Depends(admin)):
    return change(name, "promote", principal)


@router.post("/admin/agents/{name}/rollback")
def rollback(name: str, principal: Principal = Depends(admin)):
    return change(name, "rollback", principal)
