"""完整 /chat 链路（真实 FastAPI、fakeredis、SQLite）：多轮历史以消息形式到达模型，system 不随轮次变化。"""
import asyncio

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart

from tests.support.application import application
from tests.support.fake_model import scripted_model

LONG_ANSWER = "请先确认账号未锁定。" + "补充说明。" * 200


def recording_model(calls):
    async def reply(messages, info):
        if info.output_tools:  # 意图识别
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {"intent": "vpn_issue", "confidence": .9})])
        calls.append(messages)
        return ModelResponse(parts=[TextPart(LONG_ANSWER if len(calls) == 1 else "好的，已记录。")])
    return scripted_model(reply)


def test_second_turn_sees_first_turn_as_messages_with_complete_answer():
    calls = []

    async def run():
        async with application(model=recording_model(calls)) as (http, _state):
            for i, message in enumerate(["VPN 报错 VPN-691", "还是不行"]):
                response = await http.post("/chat", json={"message": message, "conv_id": "h", "client_request_id": f"h{i}"})
                assert response.status_code == 200, response.text

    asyncio.run(run())
    first_turn, second_turn = calls
    history = second_turn[:-1]
    assert isinstance(history[0], ModelRequest) and history[0].parts[0].content == first_turn[-1].parts[0].content
    assert isinstance(history[1], ModelResponse) and history[1].parts[0].content == LONG_ANSWER
    current = [p for p in second_turn[-1].parts if isinstance(p, UserPromptPart)][0].content
    assert current.endswith("[用户消息]\n还是不行") and "[最近对话]" not in current and "VPN 报错 VPN-691" not in current
    assert first_turn[-1].instructions == second_turn[-1].instructions


def test_masked_http_prefix_summary_and_provider_usage(monkeypatch):
    import json
    import re
    import httpx2
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider
    from servicedesk_agent.agent.masking.model_masking import MaskingTransport
    from tests.conftest import with_read_tool
    from tests.test_model_masking import completion

    async def run():
        calls = []
        email = "cache-user@example.org"
        def respond(request):
            body = json.loads(request.content)
            assert email not in request.content.decode()
            calls.append(body)
            answer = "压缩测试交接" if "总结交接资料" in body["messages"][-1]["content"] else LONG_ANSWER
            if len(calls) == 2:
                answer += json.loads(body["messages"][-2]["tool_calls"][0]["function"]["arguments"])["code"]
            result = completion({"role": "assistant", "content": answer})
            if len(calls) == 1:
                alias = re.search(r"\{\{EMAIL_[a-f0-9]+\}\}", body["messages"][-1]["content"])[0]
                result = completion({"role": "assistant", "tool_calls": [{"id": "lookup-1", "type": "function",
                    "function": {"name": "diagnose", "arguments": json.dumps({"code": alias})}}]})
            result["usage"] = {"prompt_tokens": 300, "completion_tokens": 20, "total_tokens": 320,
                               "prompt_cache_hit_tokens": 100}
            return httpx2.Response(200, json=result)

        transport = MaskingTransport(httpx2.MockTransport(respond))
        async with httpx2.AsyncClient(transport=transport) as client:
            model = OpenAIChatModel("test", provider=OpenAIProvider(
                base_url="http://provider.test/v1", api_key="synthetic", http_client=client))
            async with application(model=model) as (http, state):
                monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
                with_read_tool(state.orchestrator)
                state.memory.COMPRESS_AT = 8
                for i in range(6):
                    text = f"VPN 报错 VPN-691 联系 {email}，密码=NeverStore123" if i == 0 else f"VPN-691 还不行 {i}"
                    response = await http.post("/chat", json={"message": text, "conv_id": "prefix", "client_request_id": str(i)})
                    body = response.json()
                    assert body["status"] == "success" and body["token_usage"]["cache_hit_rate"] == 0.333333
                    trace = await state.memory.get_trace(body["request_id"])
                    assert trace["observability"]["cache_hit_rate"] == 0.333333
                    if i == 0:
                        keys = await state.memory._redis.keys("servicedesk_agent:wm_v2:*")
                        records = await state.memory._redis.lrange(keys[0], 0, -1)
                        assert "NeverStore123" not in "".join(records)
                        assert "model_messages" in json.loads(records[0])["metadata"]
                        assert "new_messages" not in body and "new_messages" not in trace
                first, tool_round, second = calls[:3]
                alias = json.loads(tool_round["messages"][-2]["tool_calls"][0]["function"]["arguments"])["code"]
                prefix = tool_round["messages"] + [{"role": "assistant", "content": LONG_ANSWER + alias}]
                assert [m["role"] for m in prefix[-4:]] == ["user", "assistant", "tool", "assistant"]
                assert second["messages"][:-1] == prefix
                original_bytes = json.dumps(tool_round["messages"], ensure_ascii=False).encode()
                assert json.dumps(second["messages"][:-2], ensure_ascii=False).encode() == original_bytes
                assert "{{EMAIL_" in first["messages"][-1]["content"]
                assert second["tools"] == first["tools"]
                assert second["messages"][:len(first["messages"])] == first["messages"]
                summary, = [call for call in calls if "总结交接资料" in call["messages"][-1]["content"]]
                assert summary["tools"] == first["tools"]
                assert summary["messages"][:len(prefix)] == prefix
                for after in calls[-2:]:
                    assert after["messages"][1]["content"].endswith("压缩测试交接")
                    assert "压缩测试交接" not in after["messages"][-1]["content"]
                assert json.dumps(calls[-2]["messages"][:-1]).encode() == json.dumps(calls[-1]["messages"][:-3]).encode()
                assert all(message["role"] != "tool" for message in calls[-1]["messages"])
    asyncio.run(run())
