#!/usr/bin/env python3
"""Phase 3 memory quality smoke metrics over real Redis + Persistent ChromaDB."""
from __future__ import annotations
import asyncio, json, os, pathlib, shutil, sys, tempfile
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from memory.conversation_memory import MemoryManager

async def main():
    root = pathlib.Path(tempfile.mkdtemp(prefix="it-copilot-memory-eval-"))
    manager = MemoryManager(redis_url=os.getenv("REDIS_URL", "redis://:it-copilot123@127.0.0.1:6379/0"), chroma_host="127.0.0.1", chroma_port=8999, chroma_path=str(root / "chroma"), api_key="sk-local")
    user, conv = "phase3-eval-user", "historical-conversation"
    try:
        await manager.store_episodic_turn(user, conv, "mem-net", "VPN 错误码 VPN-1002", "网络排障建议", {"intent":"vpn_issue", "intent_group":"network", "entities":{"error_code":["VPN-1002"]}})
        await manager.store_episodic_turn(user, conv, "mem-access", "申请 GitLab 权限", "权限审批建议", {"intent":"permission_request", "intent_group":"access", "entities":{"system":["GitLab"]}})
        network_hits = await manager._search_episodic(user, "new-conversation", "VPN 错误码", intent_group="network", max_tokens=500)
        access_hits = await manager._search_episodic(user, "new-conversation", "GitLab 权限", intent_group="access", max_tokens=500)
        metrics = {"episodic_recall": float(bool(network_hits)), "intent_filtered_recall": float(bool(access_hits)), "unrelated_injection_rate": float(any("GitLab" in item for item in network_hits)), "preference_hit": 0.0}
        profile = manager._normalize_profile({"department":"研发", "systems":["GitLab"]})
        await asyncio.to_thread(manager._profile.upsert, ids=[manager._profile_doc_id(user)], documents=[json.dumps(profile, ensure_ascii=False)], metadatas=[{"user_id":user,"updated_at":"2026-10-25T00:00:00"}])
        loaded = await manager._get_profile(user)
        metrics["preference_hit"] = float("GitLab" in loaded.get("systems", []))
        report={"suite":"memory-v1", "total":4, "metrics":metrics, "status":"passed" if metrics["episodic_recall"] and metrics["intent_filtered_recall"] and not metrics["unrelated_injection_rate"] and metrics["preference_hit"] else "failed"}
        output=ROOT/"evaluation/reports/memory_latest.json"; output.parent.mkdir(exist_ok=True); output.write_text(json.dumps(report,ensure_ascii=False,indent=2)); print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        await manager.close(); shutil.rmtree(root, ignore_errors=True)

if __name__ == "__main__":
    asyncio.run(main())
