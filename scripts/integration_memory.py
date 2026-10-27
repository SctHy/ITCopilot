#!/usr/bin/env python3
"""Real Redis + local Persistent ChromaDB Phase 3 smoke/integration test."""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import tempfile
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memory.conversation_memory import MemoryManager, MsgRole


async def main() -> None:
    root = pathlib.Path(tempfile.mkdtemp(prefix="it-copilot-memory-"))
    redis_url = os.getenv("REDIS_URL", "redis://:it-copilot123@127.0.0.1:6379/0")
    manager = MemoryManager(
        redis_url=redis_url,
        chroma_host=os.getenv("CHROMA_HOST", "127.0.0.1"),
        chroma_port=int(os.getenv("CHROMA_PORT", "8999")),
        chroma_path=str(root / "chroma"),
        api_key=os.getenv("ANTHROPIC_API_KEY", "sk-local"),
        model=os.getenv("ANTHROPIC_MODEL", "deepseek-chat"),
    )
    user_id, old_conv = "phase3-integration-user", "phase3-old-conversation"
    try:
        await manager.add_message(user_id, old_conv, MsgRole.USER, "我在研发部门，常用 GitLab，设备号 DEV-100")
        await manager.add_message(user_id, old_conv, MsgRole.ASSISTANT, "已记录你的 IT 偏好。")
        await manager.store_episodic_turn(
            user_id, old_conv, "phase3-req-1", "VPN 连不上，错误码 VPN-1002", "请检查网络并提供错误码。",
            {"intent": "vpn_issue", "intent_group": "network", "agent_type": "network_software", "entities": {"error_code": ["VPN-1002"]}},
        )
        await manager.store_episodic_turn(
            user_id, old_conv, "phase3-req-2", "申请 GitLab 权限", "需要提交审批。",
            {"intent": "permission_request", "intent_group": "access", "agent_type": "access", "entities": {"system": ["GitLab"]}},
        )
        profile = manager._normalize_profile({"department": "研发", "systems": ["GitLab"], "devices": ["DEV-100"]})
        await asyncio.to_thread(manager._profile.upsert, ids=[manager._profile_doc_id(user_id)], documents=[json.dumps(profile, ensure_ascii=False)], metadatas=[{"user_id": user_id, "updated_at": "2026-10-25T00:00:00"}])
        context = await manager.get_context(user_id, "phase3-new-conversation", "VPN 错误码 VPN-1002", max_history_tokens=500)
        assert context.user_profile["department"] == "研发"
        assert "GitLab" in context.user_profile["systems"]
        assert context.relevant_history
        assert len(" ".join(context.relevant_history)) // 2 <= 500
        print(json.dumps({"redis_working_messages": len(await manager._get_working_memory(user_id, old_conv)), "profile_schema": context.user_profile["schema_version"], "history_hits": len(context.relevant_history), "status": "passed"}, ensure_ascii=False))
    finally:
        await manager.close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
