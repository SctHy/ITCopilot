"""离线测试与基准用的假模型：同一个 reply 函数同时支持普通请求与流式请求。"""
from __future__ import annotations

import json

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel


STREAM_CHUNK = 8  # 流式时每段文字的长度；测试可改它来覆盖不同切分


def scripted_model(reply) -> FunctionModel:
    """reply(messages, info) -> ModelResponse；流式时把文本切成小段、工具调用整段吐出。"""
    async def stream(messages, info):
        response: ModelResponse = await reply(messages, info)
        for index, part in enumerate(response.parts):
            if isinstance(part, TextPart):
                for start in range(0, len(part.content), STREAM_CHUNK):
                    yield part.content[start:start + STREAM_CHUNK]
            elif isinstance(part, ToolCallPart):
                args = part.args if isinstance(part.args, str) else json.dumps(part.args or {}, ensure_ascii=False)
                yield {index: DeltaToolCall(name=part.tool_name, json_args=args)}

    return FunctionModel(reply, stream_function=stream)
