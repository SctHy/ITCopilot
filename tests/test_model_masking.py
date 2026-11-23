"""检查真正到达 HTTP transport 的 payload 与本地工具执行，不调用公网模型。"""
import asyncio
import json

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from servicedesk_agent.agent.llm import build_model, close_model, complete_text
from servicedesk_agent.agent.masking.model_masking import MaskingError, MaskingSession, MaskingTransport, mask_request, masking_scope


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
    monkeypatch.setenv("SERVICEDESK_AGENT_MASK_WORDS", '["Project Nightjar"]')
    body = {"messages": [{"role": "user", "content": "部署 Project Nightjar"}]}
    result = mask_request(body, MaskingSession())
    assert "Project Nightjar" not in json.dumps(result)
    monkeypatch.setenv("SERVICEDESK_AGENT_MASK_WORDS", '"not a list"')
    with pytest.raises(ValueError, match="JSON array"):
        mask_request(body, MaskingSession())


def test_short_password_does_not_replace_normal_words():
    session = MaskingSession()
    source = "password=a; a normal app"
    session.learn(source)
    assert session.mask(source).endswith("; a normal app")


def test_classifier_state_is_masked():
    from servicedesk_agent.agent.intent.recognizer import IntentRecognizer

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
    from servicedesk_agent.agent.masking.model_masking import _TextBuffer
    session = MaskingSession()
    raw = 'quoted"back\\slash'
    token = session.remember(raw, "SECRET")
    for split in range(len(token) + 1):
        buf = _TextBuffer(session, tool=tool)
        got = buf.feed(token[:split]) + buf.feed(token[split:])
        assert not buf.pending
        assert got == (json.dumps(raw)[1:-1] if tool else "[REDACTED]")


def test_stream_plain_brace_and_incomplete_placeholder():
    from servicedesk_agent.agent.masking.model_masking import _ResponseStream
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
    from servicedesk_agent.agent.masking.redaction import redact_text
    assert redact_text("ssh git@github.com:org/project.git") == "ssh git@github.com:org/project.git"


def test_restored_tool_values_are_scrubbed_when_stored():
    from servicedesk_agent.agent.masking.redaction import redact
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


@pytest.mark.parametrize("field", ["input", "messages"])
def test_masking_key_and_forward_only_expansion(monkeypatch, field):
    from servicedesk_agent.agent.masking.local_pii import Span
    from servicedesk_agent.agent.masking.model_masking import _masking_key

    monkeypatch.setenv("SERVICEDESK_AGENT_MASKING_KEY", "synthetic-test-key")
    assert len(_masking_key()) == 32 and _masking_key() == _masking_key()
    monkeypatch.delenv("SERVICEDESK_AGENT_MASKING_KEY")
    assert _masking_key() != _masking_key()
    session = MaskingSession("conversation")
    value = "Sample Person"
    declaration = "Contact " + value
    session.findings = {declaration: [Span(8, len(declaration), "PERSON", 1)]}
    body = {field: [{"role": "user", "content": "Project " + value}]}
    first = mask_request(body, session)[field][0]
    body[field].extend([{"role": "assistant", "content": declaration},
                             {"role": "user", "content": "Ask " + value}])
    masked = mask_request(body, session)[field]
    assert first == masked[0] and value in first["content"]
    assert value not in masked[1]["content"] and value not in masked[2]["content"]


def test_anthropic_breakpoints_at_stable_boundaries():
    from servicedesk_agent.agent.masking.model_masking import _cache_breakpoints
    messages = [{"role": role, "content": "text"} for role in ("system", "user", "assistant", "user")]
    _cache_breakpoints(messages)
    assert messages[0]["content"] == messages[2]["content"] == [
        {"type": "text", "text": "text", "cache_control": {"type": "ephemeral"}}]
    assert messages[1]["content"] == messages[3]["content"] == "text"


@pytest.mark.parametrize("stream", [False, True])
def test_responses_roundtrip(stream):
    email = "alice@example.org"
    usage = {"input_tokens": 42, "output_tokens": 7, "input_tokens_details": {"cached_tokens": 32}}
    def upstream(request):
        assert email not in request.content.decode()
        token = json.loads(request.content)["instructions"]
        assert token.startswith("{{")
        args = json.dumps({"email": token})
        output = [{"type": "function_call", "arguments": args},
                  {"type": "reasoning", "summary": [{"type": "summary_text", "text": token}]},
                  {"type": "message", "content": [{"type": "output_text", "text": token}]}]
        response = {"output": output, "usage": usage}
        if not stream:
            return httpx2.Response(200, json=response)
        events = []
        for family, field, value in [("output_text", "text", token), ("reasoning_summary_text", "text", token),
                                     ("function_call_arguments", "arguments", args)]:
            events.extend({"type": f"response.{family}.delta", "delta": ch} for ch in value)
            events.append({"type": f"response.{family}.done", field: value})
        events.extend([{"type": "response.output_item.done", "item": output[0]},
                       {"type": "response.content_part.done", "part": output[-1]["content"][0]},
                       {"type": "response.completed", "response": response}])
        wire = "".join(f"event: {e['type']}\r\ndata: {json.dumps(e)}\r\n\r\n" for e in events)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=Bytes(wire, 1))

    async def run():
        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
            with masking_scope("owner", "conv") as session:
                res = await client.post("https://provider.test/v1/responses", json={
                    "instructions": email, "tools": [{"type": "function", "name": "lookup", "description": email}],
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": email}]},
                              {"type": "function_call", "arguments": json.dumps({"email": email})},
                              {"type": "function_call_output", "output": email}]})
                if stream:
                    events = []
                    for block in res.text.strip().split("\n\n"):
                        name, data = block.splitlines()
                        event = json.loads(data.removeprefix("data: "))
                        assert name == "event: " + event["type"]
                        events.append(event)
                    for family in ("output_text", "reasoning_summary_text", "function_call_arguments"):
                        deltas = "".join(e["delta"] for e in events if e["type"] == f"response.{family}.delta")
                        assert deltas == (json.dumps({"email": email}) if family == "function_call_arguments" else "[REDACTED]")
                    body = events[-1]["response"]
                else:
                    body = res.json()
                call, reasoning, message = body["output"]
                assert json.loads(call["arguments"]) == {"email": email}
                assert reasoning["summary"][0]["text"] == message["content"][0]["text"] == "[REDACTED]"
                assert body["usage"] == usage
                assert session.provider_text.getvalue() == session.reverse[email]
            with pytest.raises(MaskingError):
                await client.post("https://provider.test/v1/responses", json={"input": [
                    {"role": "user", "content": [{"type": "input_image", "image_url": "url"}]}]})
    asyncio.run(run())


def test_responses_sdk_reasoning_tools_and_cache_usage():
    from pydantic_ai.models.openai import OpenAIResponsesModel

    email = "alice@example.org"
    usage = {"input_tokens": 42, "output_tokens": 7, "total_tokens": 49,
             "input_tokens_details": {"cached_tokens": 32}, "output_tokens_details": {"reasoning_tokens": 1}}
    async def run():
        requests, invoked = [], []
        def upstream(req):
            body = json.loads(req.content)
            requests.append(body)
            assert req.url.path == "/v1/responses" and email not in req.content.decode()
            assert body["reasoning"]["effort"] == "high" and body["store"] is False
            assert body["prompt_cache_key"] == "session" and body["tools"]
            token = body["input"][0]["content"]
            if isinstance(token, list):
                token = token[0]["text"]
            if len(requests) == 1:
                item = {"type": "function_call", "name": "lookup", "call_id": "call_1",
                        "arguments": json.dumps({"email": token}), "id": "fc_1"}
            else:
                item = {"type": "message", "role": "assistant", "id": "msg_1", "status": "completed",
                        "content": [{"type": "output_text", "text": token, "annotations": []}]}
            return httpx2.Response(200, json={"id": "resp_1", "object": "response", "created_at": 1,
                "status": "completed", "model": "gpt-6.2-sol", "output": [item], "usage": usage})
        async with httpx2.AsyncClient(transport=MaskingTransport(httpx2.MockTransport(upstream))) as client:
            model = OpenAIResponsesModel("gpt-6.2-sol", provider=OpenAIProvider(
                api_key="synthetic", base_url="https://provider.test/v1", http_client=client))
            agent = Agent(model)
            @agent.tool_plain
            def lookup(email: str) -> str:
                invoked.append(email)
                return email
            with masking_scope("owner", "conv"):
                result = await agent.run(email, model_settings={"openai_reasoning_effort": "high",
                    "openai_store": False, "openai_prompt_cache_key": "session"})
            assert invoked == [email] and result.output == "[REDACTED]"
            assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.cache_read_tokens) == (84, 14, 64)
    asyncio.run(run())


def test_responses_masked_prefix_across_turns_stream_and_summary(monkeypatch):
    from pydantic_ai.direct import model_request_stream
    from pydantic_ai.messages import ModelRequest
    from servicedesk_agent.agent.model_router import ModelRouter

    calls = []
    email = "prefix@example.org"
    instructions = f"Contact {email}"
    response = {"id": "resp_1", "object": "response", "created_at": 1, "status": "completed",
                "model": "gpt-6.2-sol", "output": [{"type": "message", "role": "assistant", "id": "msg_1",
                "status": "completed", "content": [{"type": "output_text", "text": "OK", "annotations": []}]}]}

    def upstream(req):
        assert req.url.path == "/v1/responses" and email not in req.content.decode()
        body = json.loads(req.content)
        assert "instructions" not in body and body["prompt_cache_retention"] == "24h"
        calls.append(body)
        if body.get("stream"):
            event = {"type": "response.created", "sequence_number": 0, "response": response}
            return httpx2.Response(200, headers={"content-type": "text/event-stream"},
                                   stream=Bytes(f"event: response.created\ndata: {json.dumps(event)}\n\n"))
        return httpx2.Response(200, json=response)

    monkeypatch.setenv("OPENAI_GPT_API_KEY", "synthetic")
    monkeypatch.setenv("OPENAI_GPT_BASE_URL", "https://provider.test/v1")
    monkeypatch.setattr("servicedesk_agent.agent.llm.MaskingTransport",
                        lambda **kwargs: MaskingTransport(httpx2.MockTransport(upstream), **kwargs))

    async def run():
        router = ModelRouter()
        try:
            choice = router.pick("agent_read", model="gpt-6.2-sol", conv_id="conv")
            agent = Agent(choice.model, instructions=instructions, model_settings=choice.settings)
            with masking_scope("owner", "conv") as session:
                first = await agent.run("first")
                history = first.all_messages()
                await agent.run("second", message_history=history)
                await complete_text(choice.model, "summarize", history=history, prefix=(instructions, []), max_tokens=80)
                async with model_request_stream(choice.model, [*history, ModelRequest.user_text_prompt(
                        "stream", instructions=instructions)], model_settings=choice.settings):
                    pass
                await choice.model.client.responses.create(model=choice.name, instructions=instructions,
                    input=email, prompt_cache_retention="24h")
                developer = {"role": "developer", "content": f"Contact {session.reverse[email]}"}
                assert all(call["input"][0] == developer for call in calls)
                assert calls[-1]["input"] == [developer, {"role": "user", "content": session.reverse[email]}]
        finally:
            await router.aclose()

    asyncio.run(run())
    first, second, summary, stream, _ = calls
    assert stream["stream"] is True
    assert second["input"][:len(first["input"])] == first["input"]
    assert len(second["input"][:-1]) > len(first["input"])
    prefix = json.dumps(second["input"][:-1]).encode()
    assert all(json.dumps(call["input"][:-1]).encode() == prefix for call in (summary, stream))
