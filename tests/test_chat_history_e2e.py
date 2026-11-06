"""完整 /chat 链路（真实 FastAPI、fakeredis、SQLite）：多轮历史以消息形式到达模型，system 不随轮次变化。"""
import asyncio

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart

from evaluation.application import application
from evaluation.fake_model import scripted_model

LONG_ANSWER = "请先确认账号未锁定。" + "补充说明。" * 200


def recording_model(calls):
    async def reply(messages, info):
        if info.output_tools:  # 意图识别
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {"intent": "vpn_issue", "confidence": .9})])
        calls.append(messages)
        return ModelResponse(parts=[TextPart(LONG_ANSWER if len(calls) == 1 else "好的，已记录。")])
    return scripted_model(reply)


def test_second_turn_sees_first_turn_as_messages_with_truncated_answer():
    calls = []

    async def run():
        async with application(model=recording_model(calls)) as (http, _state):
            for i, message in enumerate(["VPN 报错 VPN-691", "还是不行"]):
                response = await http.post("/chat", json={"message": message, "conv_id": "h", "client_request_id": f"h{i}"})
                assert response.status_code == 200, response.text

    asyncio.run(run())
    first_turn, second_turn = calls
    history = second_turn[:-1]
    assert isinstance(history[0], ModelRequest) and history[0].parts[0].content == "VPN 报错 VPN-691"
    assert isinstance(history[1], ModelResponse) and history[1].parts[0].content.endswith("…（已截断）")
    assert len(history[1].parts[0].content) <= 600 + len("…（已截断）")
    current = [p for p in second_turn[-1].parts if isinstance(p, UserPromptPart)][0].content
    assert current.endswith("[用户消息]\n还是不行") and "[最近对话]" not in current and "VPN 报错 VPN-691" not in current
    assert first_turn[-1].instructions == second_turn[-1].instructions
