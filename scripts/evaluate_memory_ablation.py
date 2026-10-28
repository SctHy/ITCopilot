"""Compare context availability, correction precedence and isolation (not answer quality).

Four fixtures per mode with real memory code, temporary Chroma and fakeredis.
No language model is used; these are deterministic context-contract checks.
"""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from core.conversation_state import merge_state, known_entities
from evaluation.application import application
from evaluation.reporting import write_report


async def main():
    rows = []
    async with application() as (_, api):
        memory = api._memory
        state = merge_state(
            {}, "access", {"employee_id": ["1002"]}, "old", intent="permission_request"
        )
        state = merge_state(
            state,
            "access",
            {"employee_id": ["1001"], "system": ["GitLab"]},
            "更正工号",
            intent="permission_request",
        )
        await memory.commit_turn(
            "a",
            "history",
            "first",
            "工号更正为1001",
            {"request_id": "first", "status": "success", "response": "已更正"},
            state=state,
        )
        for i in range(16):
            await memory.commit_turn(
                "a",
                "history",
                str(i),
                "其他问题",
                {"request_id": str(i), "status": "success", "response": "已收到"},
                state=state,
            )
        await memory.store_episodic_turn(
            "a",
            "history",
            "vpn",
            "VPN-809连接超时",
            "检查UDP 500与4500",
            {"intent_group": "network"},
        )
        await memory.store_episodic_turn(
            "b",
            "history",
            "private",
            "VPN-809私有记录",
            "OTHER_USER_CANARY",
            {"intent_group": "network"},
        )
        await memory.store_episodic_turn(
            "a", "history", "access", "GitLab权限", "ACCESS_ONLY_CANARY", {"intent_group": "access"}
        )
        for mode in ("working", "state", "full"):
            memory.context_mode = mode
            context = await memory.get_context(
                "a", "history", query="VPN-809", intent_group="network"
            )
            cross = await memory.get_context("a", "new", query="VPN-809", intent_group="network")
            text = cross.to_prompt_text()
            checks = {
                "corrected_entity_after_history_truncation": known_entities(context.state, {}).get(
                    "employee_id"
                )
                == ["1001"],
                "cross_conversation_recall": "UDP 500" in text,
                "cross_user_isolation": "OTHER_USER_CANARY" not in text,
                "intent_isolation": "ACCESS_ONLY_CANARY" not in text,
            }
            rows.append(
                {
                    "mode": mode,
                    "checks": checks,
                    "passed": sum(checks.values()),
                    "total": len(checks),
                    "context_chars": len(context.to_prompt_text()),
                    "episodic_hits": len(cross.relevant_history),
                }
            )
    report = {
        "suite": "memory-ablation-v2",
        "status": "complete",
        "model_calls": 0,
        "fixtures_per_mode": 4,
        "results": rows,
        "limitations": "Four controlled context fixtures, no user dialogue success-rate claim and no production-memory benchmark.",
    }
    write_report("memory_ablation_v2.json", report)


if __name__ == "__main__":
    asyncio.run(main())
