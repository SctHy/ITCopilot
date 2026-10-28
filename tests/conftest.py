"""Shared test helpers. Auth runs in dev mode: tokens are minted for the demo directory."""
import os

os.environ.setdefault("IT_COPILOT_AUTH_MODE", "dev")
os.environ.setdefault("IT_COPILOT_ENV", "development")


def auth(username: str = "alice", **overrides) -> dict:
    from core.identity import issue_dev_token

    return {"Authorization": "Bearer " + issue_dev_token(username, overrides=overrides or None)}
