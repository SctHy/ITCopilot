"""Read-only inspection of deployed Agent specifications; releases belong to deployment."""
from fastapi import APIRouter, Depends

from api import state
from api.deps import require_platform_admin

router = APIRouter()


@router.get("/admin/agents", dependencies=[Depends(require_platform_admin)])
def list_agents():
    return [{"name": spec.name, "version": spec.version, "fingerprint": spec.fingerprint,
             "serves": spec.serves.model_dump(mode="json"), "tenants": sorted(spec.tenants)}
            for spec in state.agent_registry.specs.values()]
