"""Jev（TypeSafe System One）客户端：给定状态文本与一组 choice/score/noul 题，返回带概率的结构化答案。

只适合固定选项的判断（分类、打分、是/否），不能生成自由文本。
API：https://docs.typesafe.ai/introduction/quickstart.md
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import httpx2

API_URL = "https://api.typesafe.ai/v1/systemone"


@dataclass
class JevResponse:
    answers: Dict[str, Dict[str, Any]]
    model: str = ""
    usage: Dict[str, int] = field(default_factory=dict)


class JevClient:
    """``retries`` 只对 429/5xx 生效；线上调用保持 0，失败交给调用方降级。"""

    def __init__(self, api_key: str, model: str = "jev-latest", timeout_s: float = 2., retries: int = 0, url: Optional[str] = None):
        self.model = model
        self._url = url or os.getenv("TYPESAFE_API_URL", API_URL)
        self._timeout_s = timeout_s
        self._retries = retries
        self._client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout_s)

    @classmethod
    def from_env(cls, **kwargs) -> Optional["JevClient"]:
        key = os.getenv("TYPESAFE_API_KEY")
        return cls(key, model=os.getenv("TYPESAFE_MODEL", "jev-latest"), **kwargs) if key else None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ask(self, state: str, questions: Dict[str, Dict[str, Any]]) -> JevResponse:
        payload = {"model": self.model, "state": state, "questions": questions}
        for attempt in range(self._retries + 1):
            # 连接池排队不受 httpx 超时约束，整体再套一层 wait_for 保证时延上限。
            response = await asyncio.wait_for(self._client.post(self._url, json=payload), timeout=self._timeout_s)
            retryable = response.status_code == 429 or response.status_code >= 500
            if not retryable or attempt == self._retries:
                response.raise_for_status()
                data = response.json()
                return JevResponse(answers=data["answers"], model=data.get("model", ""), usage=data.get("usage") or {})
            await asyncio.sleep(2 ** attempt)
        raise RuntimeError("unreachable")


def top_choice(answer: Dict[str, Any]) -> tuple[str, float]:
    """choice 答案的选项与其概率（没有概率时退回 confidence）。"""
    choice = answer["choice"]
    return choice, float((answer.get("probabilities") or {}).get(choice, answer.get("confidence", 0.)))
