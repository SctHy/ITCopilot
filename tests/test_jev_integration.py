"""Jev 接入点：结果重排后端。外部 API 一律用假客户端替代。"""
import asyncio

from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from core.jev import JevResponse
from tooling.tool_manager import MCPToolManager


class FakeJev:
    def __init__(self, answers=None, error=None):
        self.answers, self.error, self.calls = answers, error, []

    async def ask(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        return JevResponse(answers=self.answers(questions) if callable(self.answers) else self.answers)


def test_jev_rerank_orders_by_relevance_and_keeps_recall_order_on_ties():
    scores = {"c0": .1, "c1": .9, "c2": .1, "c3": .6}

    async def run():
        jev = FakeJev(lambda questions: {k: {"type": "noul", "noul": scores[k]} for k in questions})
        manager = MCPToolManager("sk-local", jev=jev)
        ranked, reranked = await manager._rerank_with_status("vpn", ["a", "b", "c", "d"], 3)
        assert reranked and ranked == ["b", "d", "a"]
        state, questions = jev.calls[0]
        assert state == "用户问题：vpn" and len(questions) == 4 and '"b"' in questions["c1"]["instructions"]

    asyncio.run(run())


def test_jev_rerank_failure_falls_back_to_llm():
    async def respond(messages, info):
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"response": [2, 0, 1]})])

    async def run():
        manager = MCPToolManager("sk-local", jev=FakeJev(error=TimeoutError()))
        manager._llm = FunctionModel(respond)
        assert await manager._rerank_with_status("vpn", ["a", "b", "c"], 2) == (["c", "a"], True)

    asyncio.run(run())
