#!/usr/bin/env python3
"""Phase 3 记忆质量离线评测（临时 Persistent ChromaDB）：情景召回、意图过滤、无关注入与画像命中。"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import shutil
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from evaluation.reporting import status, write_report
from memory.conversation_memory import MemoryManager

USER, HISTORY_CONV, NEW_CONV = "phase3-eval-user", "historical-conversation", "new-conversation"


async def main():
    root = pathlib.Path(tempfile.mkdtemp(prefix="it-copilot-memory-eval-"))
    manager = MemoryManager(redis_url=os.getenv("REDIS_URL", "redis://:it-copilot123@127.0.0.1:6379/0"), chroma_host="127.0.0.1", chroma_port=8999, chroma_path=str(root / "chroma"), api_key="sk-local")
    try:
        await manager.store_episodic_turn(USER, HISTORY_CONV, "mem-net", "VPN 错误码 VPN-1002", "网络排障建议", {"intent": "vpn_issue", "intent_group": "network", "entities": {"error_code": ["VPN-1002"]}})
        await manager.store_episodic_turn(USER, HISTORY_CONV, "mem-access", "申请 GitLab 权限", "权限审批建议", {"intent": "permission_request", "intent_group": "access", "entities": {"system": ["GitLab"]}})
        network_hits = await manager._search_episodic(USER, NEW_CONV, "VPN 错误码", intent_group="network", max_tokens=500)
        access_hits = await manager._search_episodic(USER, NEW_CONV, "GitLab 权限", intent_group="access", max_tokens=500)

        profile = manager._normalize_profile({"department": "研发", "systems": ["GitLab"]})
        await asyncio.to_thread(manager._profile.upsert, ids=[manager._profile_doc_id(USER)], documents=[json.dumps(profile, ensure_ascii=False)], metadatas=[{"user_id": USER, "updated_at": "2026-10-25T00:00:00"}])
        loaded = await manager._get_profile(USER)

        metrics = {
            "episodic_recall": float(bool(network_hits)),
            "intent_filtered_recall": float(bool(access_hits)),
            "unrelated_injection_rate": float(any("GitLab" in item for item in network_hits)),
            "preference_hit": float("GitLab" in loaded.get("systems", [])),
        }
        ok = metrics["episodic_recall"] and metrics["intent_filtered_recall"] and not metrics["unrelated_injection_rate"] and metrics["preference_hit"]
        write_report("memory_latest.json", {"suite": "memory-v1", "total": len(metrics), "metrics": metrics, "status": status(bool(ok))})
    finally:
        await manager.close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
