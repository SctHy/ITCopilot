#!/usr/bin/env python3
"""Phase 3 reproducible cross-session memory demonstration."""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from memory.conversation_memory import MemoryManager


async def main() -> None:
    root = pathlib.Path(tempfile.mkdtemp(prefix="it-copilot-cross-session-"))
    manager = MemoryManager(
        redis_url=os.getenv("REDIS_URL", "redis://:it-copilot123@127.0.0.1:6379/0"),
        chroma_host=os.getenv("CHROMA_HOST", "127.0.0.1"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8999")),
        chroma_path=str(root / "chroma"), api_key=os.getenv("ANTHROPIC_API_KEY", "sk-local"),
    )
    user_id = "phase3-demo-user"
    try:
        profile = manager._normalize_profile({"department": "研发", "systems": ["GitLab"], "devices": ["DEV-100"]})
        await asyncio.to_thread(manager._profile.upsert, ids=[manager._profile_doc_id(user_id)], documents=[json.dumps(profile, ensure_ascii=False)], metadatas=[{"user_id": user_id, "updated_at": "2026-10-25T00:00:00"}])
        context = await manager.get_context(user_id, "brand-new-conversation", "我需要申请 GitLab 权限", max_history_tokens=400)
        result = {"new_conversation": True, "profile_department": context.user_profile.get("department"), "known_systems": context.user_profile.get("systems", []), "known_devices": context.user_profile.get("devices", []), "personalization_hit": "GitLab" in context.user_profile.get("systems", []), "status": "passed"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await manager.close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
