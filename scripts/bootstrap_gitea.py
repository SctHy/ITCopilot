"""Idempotently prepare the Gitea sandbox used as the real permission system for `gitlab`.

Creates the sandbox org + private repo and one Gitea account per directory employee
(usernames from workflow.store.SEED_EMPLOYEES). The service account itself is created
by the `gitea-init` compose job via `gitea admin user create`.

    IT_COPILOT_GITEA_URL=http://127.0.0.1:3001 IT_COPILOT_GITEA_USER=itc-svc \
    IT_COPILOT_GITEA_PASSWORD=... python scripts/bootstrap_gitea.py
"""
from __future__ import annotations

import os
import pathlib
import secrets
import sys
import time

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from workflow.store import SEED_EMPLOYEES  # noqa: E402


def main() -> int:
    base = os.environ.get("IT_COPILOT_GITEA_URL", "http://127.0.0.1:3001").rstrip("/") + "/api/v1"
    auth = (os.environ.get("IT_COPILOT_GITEA_USER", "itc-svc"), os.environ["IT_COPILOT_GITEA_PASSWORD"])
    repos = dict(item.split("=", 1) for item in os.getenv("IT_COPILOT_GITEA_REPOS", "gitlab=it-sandbox/platform").split(",") if "=" in item)
    with httpx.Client(base_url=base, auth=auth, timeout=10) as http:
        for _ in range(60):
            try:
                if http.get("/version").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(2)
        else:
            print("gitea not reachable", file=sys.stderr)
            return 1
        for _, username, _ in SEED_EMPLOYEES:
            response = http.post("/admin/users", json={"username": username, "email": f"{username}@example.com",
                                                       "password": secrets.token_urlsafe(16), "must_change_password": False})
            assert response.status_code in (201, 422), response.text  # 422: already exists
        for owner_repo in repos.values():
            owner, repo = owner_repo.split("/", 1)
            response = http.post("/orgs", json={"username": owner, "visibility": "private"})
            assert response.status_code in (201, 422), response.text
            response = http.post(f"/orgs/{owner}/repos", json={"name": repo, "private": True, "auto_init": True})
            assert response.status_code in (201, 409), response.text
            print(f"ready: {owner}/{repo}")
    print(f"ready: {len(SEED_EMPLOYEES)} sandbox accounts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
