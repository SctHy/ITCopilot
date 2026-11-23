"""检查真正到达 HTTP transport 的 payload 与本地工具执行，不调用公网模型。"""
import asyncio
import json

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from it_copilot.agent.llm import build_model, close_model, complete_text
from it_copilot.agent.masking.model_masking import MaskingError, MaskingSession, MaskingTransport, mask_request, masking_scope


class Bytes(httpx2.AsyncByteStream):
    def __init__(self, text, size=7):
        self.raw, self.size, self.closed = text.encode(), size, False

    async def __aiter__(self):
        for i in range(0, len(self.raw), self.size):
            yield self.raw[i:i + self.size]

    async def aclose(self):
        self.closed = True


def completion(message):
    return {"id": "chat_13812345678", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if message.get("tool_calls") else "stop", "message": message}]}


def event(delta, finish=None):
    return "data: " + json.dumps({"id": "chat_13812345678", "object": "chat.completion.chunk", "created": 0,
        "model": "test", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\r\n\r\n"


def test_factory_installs_transport():
    async def run():
        model = build_model(api_key="synthetic", base_url="http://unused.test/v1")
        assert isinstance(model.client._client._transport, MaskingTransport)
        await close_model(model)
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
def test_real_sdk_agent_tool_roundtrip_and_remasking(stream):
    async def run():
        seen, invoked, streams = [], [], []
        email = "alice@example.com"
        def upstream(request):
            body = json.loads(request.content)
            seen.append(body)
            assert email not in request.content.decode()
            assert body["tools"][0]["function"]["name"] == "lookup"
            prompt = body["messages"][0]["content"]
            token = prompt[prompt.index("{{"):prompt.index("}}") + 2]
            if body["messages"][-1]["role"] == "tool":
                message = {"role": "assistant", "content": "已查到 " + token}
                if stream:
                    raw = "".join(event({"content": c}) for c in message["content"]) + event({}, "stop") + "data: [DONE]\n\n"
                else:
                    return httpx2.Response(200, json=completion(message))
            else:
                args = json.dumps({"email": token})
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_13812345678", "type": "function", "function": {"name": "lookup", "arguments": args}}]}
                if stream:
                    raw = event({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_13812345678", "type": "function", "function": {"name": "lookup", "arguments": ""}}]})
                    raw += "".join(event({"tool_calls": [{"index": 0, "function": {"arguments": c}}]}) for c in args)
                    raw += event({}, "tool_calls") + "data: [DONE]\n\n"
                else:
                    return httpx2.Response(200, json=completion(message))
            content = Bytes(raw, size=1)
            streams.append(content)
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=content)
        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
            model = OpenAIChatModel("test", provider=OpenAIProvider(base_url="http://provider.test/v1", api_key="synthetic", http_client=client))
            agent = Agent(model)
            @agent.tool_plain
            def lookup(email: str) -> str:
                invoked.append(email)
                return "员工邮箱：" + email
            with masking_scope("tenant:alice", "conversation"):
                if stream:
                    async with agent.run_stream("查询 " + email) as result:
                        chunks = [chunk async for chunk in result.stream_text(delta=True)]
                    output = "".join(chunks)
                else:
                    output = (await agent.run("查询 " + email)).output
            assert invoked == [email]
            assert output == "已查到 [REDACTED]"
            assert len(seen) == 2
            assert all(s.closed for s in streams)
            assert seen[1]["messages"][-1]["tool_call_id"] == "call_13812345678"
    asyncio.run(run())


def test_scope_stability_isolation_and_cleanup():
    with masking_scope("tenant-a:alice", "one") as a:
        a.learn("alice@example.com")
        token = a.mask("alice@example.com")
        with masking_scope("tenant-a:alice", "one") as nested:
            assert nested is a
        for owner, conv in [("tenant-b:alice", "one"), ("tenant-a:bob", "one"), ("tenant-a:alice", "two")]:
            with masking_scope(owner, conv) as b:
                b.learn("alice@example.com")
                assert b.mask("alice@example.com") != token
                with pytest.raises(MaskingError):
                    b.restore(token, tool=True)
    assert a.values == {} and a.reverse == {}
    with masking_scope("tenant-a:alice", "one") as a:
        a.learn("alice@example.com")
        assert a.mask("alice@example.com") == token


def test_parallel_requests_do_not_share_values():
    async def run():
        async def worker(i):
            with masking_scope(f"tenant:{i}", "one") as session:
                raw = f"user{i}@example.com"
                session.learn(raw)
                await asyncio.sleep(0)
                token = session.mask(raw)
                assert list(session.values.values()) == [raw]
                assert session.restore(token, tool=True) == raw
                return token
        tokens = await asyncio.gather(*(worker(i) for i in range(32)))
        assert len(set(tokens)) == 32
    asyncio.run(run())


def test_json_escaping_and_early_bare_value():
    session = MaskingSession()
    raw = 'special"quote\\line\nvalue'
    body = {"messages": [{"role": "user", "content": raw}, {"role": "tool", "tool_call_id": "keep", "content": json.dumps({"password": raw})}]}
    masked = mask_request(body, session)
    assert masked["messages"][0]["content"].startswith("{{SECRET_")
    assert body["messages"][0]["content"] == raw
    token = session.reverse[raw]
    args = session.restore(json.dumps({"value": token}), tool=True)
    assert json.loads(args)["value"] == raw


@pytest.mark.parametrize("body", [
    {"messages": [], "functions": [{"name": "lookup"}]},
    {"messages": [], "function_call": "auto"},
    {"messages": [{"role": "function", "name": "lookup", "content": "result"}]},
    {"messages": [{"role": "assistant", "function_call": {"name": "lookup", "arguments": '{"email":"alice@example.com"}'}}]},
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/private.png"}}]}]},
    {"messages": [], "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"enum": ["alice@example.com"]}}}]},
])
def test_fail_closed_before_network(body):
    async def run():
        calls = []
        def upstream(request):
            calls.append(request)
            return httpx2.Response(200)
        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
            with pytest.raises(MaskingError):
                await client.post("http://test/v1/chat/completions", json=body)
        assert calls == []
    asyncio.run(run())


@pytest.mark.parametrize("stream", [False, True])
def test_unsupported_response_function_call_fails_closed(stream):
    async def run():
        message = {"role": "assistant", "function_call": {"name": "lookup", "arguments": '{"email":"alice@example.com"}'}}
        def upstream(request):
            if stream:
                return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                       stream=Bytes(event(message) + "data: [DONE]\n\n"))
            return httpx2.Response(200, json=completion(message))
        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
            with pytest.raises(MaskingError, match="unsupported function_call") as error:
                await client.post("http://test/v1/chat/completions", json={"messages": [{"role": "user", "content": "lookup"}]})
            assert "alice@example.com" not in str(error.value)
    asyncio.run(run())


def test_masking_exception_does_not_send_or_expose_secret(monkeypatch):
    async def run():
        calls = []
        def fail(*args):
            raise ValueError("private raw value")
        monkeypatch.setattr(MaskingSession, "learn", fail)
        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(lambda req: calls.append(req)))) as client:
            with pytest.raises(MaskingError, match="nothing sent") as exc:
                await client.post("http://test/v1/chat/completions", json={"messages": [{"role": "user", "content": "hello"}]})
        assert not calls and "private raw value" not in str(exc.value)
    asyncio.run(run())


def test_summary_uses_masked_wire_and_safe_output():
    async def run():
        def upstream(request):
            body = json.loads(request.content)
            assert "13812345678" not in request.content.decode()
            return httpx2.Response(200, json=completion({"role": "assistant", "content": body["messages"][0]["content"]}))
        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
            model = OpenAIChatModel("test", provider=OpenAIProvider(base_url="http://provider.test/v1", api_key="synthetic", http_client=client))
            result = await complete_text(model, "联系13812345678", max_tokens=100)
            assert result.text == "联系[REDACTED]"
    asyncio.run(run())


def test_protocol_ids_and_schema_description():
    body = {"messages": [{"role": "tool", "tool_call_id": "13812345678", "content": "alice@example.com"}],
        "tools": [{"type": "function", "function": {"name": "email_lookup", "description": "Contact alice@example.com", "parameters": {"type": "object", "properties": {"email": {"type": "string"}}}}}]}
    masked = mask_request(body, MaskingSession())
    assert masked["messages"][0]["tool_call_id"] == "13812345678"
    assert masked["tools"][0]["function"]["name"] == "email_lookup"
    assert "alice@example.com" not in json.dumps(masked)


def test_custom_terms_and_bad_configuration(monkeypatch):
    monkeypatch.setenv("IT_COPILOT_MASK_WORDS", '["Project Nightjar"]')
    body = {"messages": [{"role": "user", "content": "部署 Project Nightjar"}]}
    result = mask_request(body, MaskingSession())
    assert "Project Nightjar" not in json.dumps(result)
    monkeypatch.setenv("IT_COPILOT_MASK_WORDS", '"not a list"')
    with pytest.raises(ValueError, match="JSON array"):
        mask_request(body, MaskingSession())


def test_short_password_does_not_replace_normal_words():
    session = MaskingSession()
    source = "password=a; a normal app"
    session.learn(source)
    assert session.mask(source).endswith("; a normal app")


def test_classifier_state_is_masked():
    from it_copilot.agent.intent.recognizer import IntentRecognizer

    async def run():
        def upstream(request):
            body = json.loads(request.content)
            assert "alice@example.com" not in request.content.decode()
            assert "{{EMAIL_" in body["messages"][0]["content"]
            call = {"id": "classify", "type": "function", "function": {
                "name": "classify_it_request", "arguments": json.dumps({"intent": "other", "confidence": 0.9})}}
            return httpx2.Response(200, json=completion({"role": "assistant", "tool_calls": [call]}))

        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
            recognizer = IntentRecognizer("synthetic")
            provider = OpenAIProvider(base_url="https://model.test/v1", api_key="synthetic", http_client=client)
            recognizer.llm = OpenAIChatModel("test", provider=provider)
            result = await recognizer.recognize("联系 alice@example.com")
            assert result.recognition_source == "llm_tool_use"
    asyncio.run(run())


@pytest.mark.parametrize("tool", [False, True])
def test_every_placeholder_split_and_escaped_restoration(tool):
    from it_copilot.agent.masking.model_masking import _TextBuffer
    session = MaskingSession()
    raw = 'quoted"back\\slash'
    token = session.remember(raw, "SECRET")
    for split in range(len(token) + 1):
        buf = _TextBuffer(session, tool=tool)
        got = buf.feed(token[:split]) + buf.feed(token[split:])
        assert not buf.pending
        assert got == (json.dumps(raw)[1:-1] if tool else "[REDACTED]")


def test_stream_plain_brace_and_incomplete_placeholder():
    from it_copilot.agent.masking.model_masking import _ResponseStream
    session = MaskingSession()
    response = httpx2.Response(200)
    stream = _ResponseStream(response, session)
    stream.event(['data: ' + json.dumps({"choices": [{"index": 0, "delta": {"content": "{"}}]})])
    out = stream.event(['data: ' + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})])
    assert json.loads(out.decode()[6:])["choices"][0]["delta"]["content"] == "{"
    stream.event(['data: ' + json.dumps({"choices": [{"index": 0, "delta": {"content": "{{EMAIL_abc"}}]})])
    with pytest.raises(MaskingError):
        stream.event(["data: [DONE]"])


def test_connection_password_does_not_swallow_hostname_or_ssh_remote():
    session = MaskingSession()
    original = "postgres://app:SuperSecret12@db.example.com/main"
    session.learn(original)
    masked = session.mask(original)
    assert masked.startswith("postgres://app:{{PASSWORD_")
    assert masked.endswith("@db.example.com/main")
    assert session.restore(masked, tool=True) == original
    from it_copilot.agent.masking.redaction import redact_text
    assert redact_text("ssh git@github.com:org/project.git") == "ssh git@github.com:org/project.git"


def test_restored_tool_values_are_scrubbed_when_stored():
    from it_copilot.agent.masking.redaction import redact
    with masking_scope("tenant:alice", "c") as session:
        session.learn("password=CustomOpaqueValue")
        assert redact({"input": {"code": "CustomOpaqueValue"}, "trace": "工具用了 CustomOpaqueValue"}) == {
            "input": {"code": "[REDACTED]"}, "trace": "工具用了 [REDACTED]"}


def test_network_client_keeps_environment_proxy_and_closes(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9876")
    monkeypatch.setenv("NO_PROXY", "")
    async def run():
        transport = MaskingTransport()
        assert any(route.matches(httpx2.URL("https://provider.test/v1/chat/completions")) and proxy is not None
                   for route, proxy in transport.client._mounts.items())
        await transport.aclose()
        assert transport.client.is_closed
    asyncio.run(run())
