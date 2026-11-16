"""Isolated live-model advisor comparison. Usage: python scripts/evaluate_backend_advice.py OUTPUT.json"""

import asyncio, json, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")
from agents.task_progress_advisor import TaskProgressAdvisor


async def main():
    progress = {
        "goal": "排查仓库访问",
        "steps": [{"title": "核对项目成员关系", "status": "working", "evidence_ids": []}],
        "blocker": "等待核查结果",
        "next_action": "等待项目负责人核查",
        "allow_reply_revision": True,
        "brief": {"reported": [], "hypotheses": [], "questions": [], "answered": []},
        "discussion_evidence": [],
    }
    results = []
    for text in [
        "收到，没有新增信息，目标和下一步均不变。",
        "收到，没有新增信息，目标和下一步均不变。",
        "收到，但现在报错变成 401，请更新判断。",
    ]:
        t = time.monotonic()
        out, usage = await TaskProgressAdvisor().suggest(
            progress,
            {"tenant": "default", "title": "排查仓库访问"},
            {"id": "u", "text": text, "kind": "requester", "actor_name": "alice"},
            "等待核查结果",
            "reply",
        )
        results.append(dict(text=text, seconds=time.monotonic() - t, usage=usage, output=out))
    Path(sys.argv[1]).write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
