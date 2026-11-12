"""20 段确定性对话验证记忆写入流水线；模型使用 FunctionModel，Redis 使用 fakeredis。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("IT_COPILOT_AUTH_MODE", "dev")
os.environ.setdefault("IT_COPILOT_ENV", "development")

import fakeredis.aioredis
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from sqlalchemy import select

from agents.intent.recognizer import IntentResult
from agents.intent.taxonomy import IntentCategory, UrgencyLevel
from api.schemas import ChatResponse
from core.db import read, vector_documents
from core.llm import complete_text
from core.redaction import redact_text
from evaluation.fake_model import scripted_model
from memory.admission import admit_turn
from memory.conversation_memory import MemoryManager, profile_text


CASES = [
    ("password", "密码是 Abc!2026，VPN报错691", "success", "vpn_issue", [], True),
    ("mobile", "联系13812345678，VPN报错691", "success", "vpn_issue", [], True),
    ("identity", "身份证11010519491231002X，VPN报错691", "success", "vpn_issue", [], True),
    ("card", "卡号4111111111111111，VPN报错691", "success", "vpn_issue", [], True),
    ("api_key", "sk-abcdefghijklmnop，VPN报错691", "success", "vpn_issue", [], True),
    ("bearer", "Bearer abc.def.ghi，VPN报错691", "success", "vpn_issue", [], True),
    ("otp", "验证码: 738192，VPN报错691", "success", "vpn_issue", [], True),
    ("business_ids", "工号1001，设备DEV-1001，VPN报错691", "success", "vpn_issue", [], True),
    ("greeting", "你好", "success", "greeting", [], False),
    ("chitchat", "今天天气不错", "success", "chitchat", [], False),
    ("out_of_scope", "帮我订票", "success", "out_of_scope", [], False),
    ("failed", "VPN报错", "failed", "vpn_issue", [], False),
    ("missing", "申请权限", "success", "permission_request", ["system"], False),
    ("no_evidence", "没有搜到的信息", "success", "vpn_issue", [], False),
    ("duplicate1", "请排查VPN错误691", "success", "vpn_issue", [], True),
    ("duplicate2", "请排查VPN错误691", "success", "vpn_issue", [], True),
    ("duplicate3", "请排查VPN错误691", "success", "vpn_issue", [], True),
]


async def evaluate(database_url: str) -> dict:
    manager = MemoryManager(database_url=database_url, api_key="offline")
    manager._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    rows = []

    async def answer(messages, info):
        return ModelResponse(parts=[TextPart("已检索VPN说明，请检查账号锁定状态。")])

    manager._llm = scripted_model(answer)
    try:
        for index, (name, message, status, category, missing, admitted) in enumerate(CASES):
            owner = "duplicates" if name.startswith("duplicate") else name
            rid = f"memory-governance-{index}"
            reply = (await complete_text(manager._llm, redact_text(message), max_tokens=300)).text
            result = ChatResponse(request_id=rid, response=reply, status=status, intent=category,
                intent_group="network", knowledge_used=admitted, tools_used=["search_knowledge_base"] if admitted else [])
            recognition = IntentResult(intent=IntentCategory(category), confidence=.9,
                intent_group="network", urgency=UrgencyLevel.NORMAL)
            state = {"missing_fields": missing}
            await manager.commit_turn(owner, "c", rid, message, result.model_dump(), state=state)
            record = admit_turn(result, recognition, state)
            assert (record is not None) == admitted
            if record is not None:
                await manager.store_episodic_turn(owner, "c", rid, message, record.answer, record.metadata)
            stored = manager._episodic.get(where={"user_id": owner})
            assert bool(stored["ids"]) == admitted
            rows.append({"id": name, "passed": True, "admitted": admitted})
        duplicates = manager._episodic.get(where={"user_id": "duplicates"})
        assert len(duplicates["ids"]) == 1 and duplicates["metadatas"][0]["seen"] == 3

        for index, department in enumerate(("研发部", "产品部")):
            async def profile_reply(messages, info):
                return ModelResponse(parts=[ToolCallPart("save_profile", {"background": department})])
            manager._llm = scripted_model(profile_reply)
            await manager.commit_turn("profile", "c", f"profile-{index}", f"我现在在{department}",
                {"request_id": f"profile-{index}", "response": "已了解", "status": "success"})
            assert await manager.update_profile("profile", "c")
            profile = await manager._get_profile("profile")
            assert profile["background"] == department
            rows.append({"id": f"department_{index}", "passed": True})
        assert "研发部" not in json.dumps(profile, ensure_ascii=False)
        old_at = (datetime.now(timezone.utc) - timedelta(days=181)).timestamp()
        old = manager._normalize_profile({"background": "过期部门"}, updated_at=old_at)
        assert profile_text(old) == ""
        rows.append({"id": "old_profile", "passed": True})
        secrets = ("Abc!2026", "13812345678", "11010519491231002X", "4111111111111111",
                   "sk-abcdefghijklmnop", "abc.def.ghi", "738192")
        with read(manager.engine) as db:
            documents = db.execute(select(vector_documents.c.document, vector_documents.c.meta)).all()
        stored_text = json.dumps([(row.document, row.meta) for row in documents], ensure_ascii=False)
        assert all(secret not in stored_text for secret in secrets)
        assert "工号1001" in stored_text and "DEV-1001" in stored_text
        return {"scope": "memory write/admission/profile pipeline; deterministic FunctionModel, real SQLite/vector embeddings, fakeredis",
                "dialogs": len(rows), "passed": sum(row["passed"] for row in rows), "cases": rows,
                "checks": {"sensitive_patterns_absent": True, "duplicates_seen": 3,
                           "department_replaced": True, "old_profile_excluded": True},
                "memory_ablation": {"status": "未运行", "reason": "本报告仅覆盖确定性治理断言；召回收益需另跑真实模型对照"}}
    finally:
        await manager.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="evaluation/reports/memory_governance_2026-11-12.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="memory-governance-") as path:
        report = asyncio.run(evaluate(f"sqlite:///{path}/memory.sqlite3"))
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"{report['passed']}/{report['dialogs']} passed: {output}")


if __name__ == "__main__":
    main()
