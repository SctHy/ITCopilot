"""Gitea repository-collaborator adapter: a real HTTP permission system for the
``gitlab`` policy system (Gitea runs locally in compose as the stand-in).

Gitea has no idempotency-key header. Collaborator PUT/DELETE are idempotent by
target state, and ``observe`` reads the collaborator list independently, which is
what reconciliation relies on after an ambiguous failure.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

import httpx

from adapters.base import AmbiguousError, PermanentError, TransientError

# Gitea only has read/write/admin; reporter collapses into read.
ROLE_TO_PERMISSION = {"readonly": "read", "reporter": "read", "developer": "write"}
PERMISSION_TO_LEVEL = {"read": "readonly", "write": "developer"}


class GiteaAdapter:
    name = "gitea"

    def __init__(self, base_url: str, token: str, repos: Dict[str, str], username_of: Callable[[str], Optional[str]],
                 *, basic_auth: Optional[tuple] = None, timeout_s: float = 10.0, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.repos = repos                     # policy system -> "owner/repo"
        self.username_of = username_of
        # Service account credentials: a scoped token in deployments, basic auth for the compose sandbox bootstrap.
        headers = {"Authorization": f"token {token}"} if token else {}
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/api/v1", headers=headers, auth=basic_auth if not token else None,
                                         timeout=timeout_s, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    def level(self, role: str) -> str:
        return PERMISSION_TO_LEVEL[ROLE_TO_PERMISSION[role]]

    def _user(self, employee_id: str) -> str:
        username = self.username_of(employee_id)
        if not username:
            raise PermanentError("员工在目录中没有对应的 Gitea 账号")
        return username

    def _repo(self, system: str) -> str:
        repo = self.repos.get(system)
        if not repo:
            raise PermanentError(f"{system} 没有映射到 Gitea 仓库")
        return repo

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # The request never reached Gitea.
            raise TransientError(f"Gitea 连接失败（{type(exc).__name__}）") from exc
        except httpx.TimeoutException as exc:
            raise AmbiguousError(f"Gitea 请求超时，结果未知（{type(exc).__name__}）") from exc
        except httpx.TransportError as exc:
            raise AmbiguousError(f"Gitea 传输中断，结果未知（{type(exc).__name__}）") from exc
        if response.status_code >= 500:
            raise AmbiguousError(f"Gitea 返回 {response.status_code}，结果未知")
        if response.status_code in (401, 403):
            raise PermanentError(f"Gitea 拒绝服务凭据（{response.status_code}）")
        return response

    async def preflight(self, action: Dict[str, Any]) -> None:
        args = action["arguments"]
        repo, username = self._repo(args["system"]), self._user(args["employee_id"])
        if (await self._request("GET", f"/users/{username}")).status_code == 404:
            raise PermanentError(f"Gitea 用户 {username} 不存在")
        if (await self._request("GET", f"/repos/{repo}")).status_code == 404:
            raise PermanentError(f"Gitea 仓库 {repo} 不存在")

    async def current_level(self, employee_id: str, system: str) -> Optional[str]:
        repo, username = self._repo(system), self._user(employee_id)
        # The permission endpoint answers "read" for anyone on a public repo, so check
        # explicit collaboration first.
        member = await self._request("GET", f"/repos/{repo}/collaborators/{username}")
        if member.status_code == 404:
            return None
        if member.status_code != 204:
            raise AmbiguousError(f"Gitea 协作者查询返回 {member.status_code}")
        response = await self._request("GET", f"/repos/{repo}/collaborators/{username}/permission")
        if response.status_code != 200:
            raise AmbiguousError(f"Gitea 权限查询返回 {response.status_code}")
        permission = response.json().get("permission")
        return PERMISSION_TO_LEVEL.get(permission, permission)

    def _target(self, action: Dict[str, Any]) -> Optional[str]:
        args = action["arguments"]
        if action["tool_name"] == "request_access":
            return self.level(args["role"])
        return args.get("baseline_level")

    async def apply(self, action_id: str, action: Dict[str, Any]) -> Dict[str, Any]:
        args = action["arguments"]
        repo, username = self._repo(args["system"]), self._user(args["employee_id"])
        target = self._target(action)
        if target is None:
            response = await self._request("DELETE", f"/repos/{repo}/collaborators/{username}")
            ok = response.status_code in (204, 404)
        else:
            permission = {"readonly": "read", "developer": "write"}[target]
            response = await self._request("PUT", f"/repos/{repo}/collaborators/{username}", json={"permission": permission})
            ok = response.status_code == 204
        if not ok:
            raise PermanentError(f"Gitea 拒绝变更（{response.status_code}）：{response.text[:200]}")
        return {"action": action["tool_name"], "employee_id": args["employee_id"], "system": args["system"],
                "external_system": "gitea", "repo": repo, "username": username, "level": target}

    async def observe(self, action_id: str, action: Dict[str, Any]) -> bool:
        args = action["arguments"]
        return await self.current_level(args["employee_id"], args["system"]) == self._target(action)
