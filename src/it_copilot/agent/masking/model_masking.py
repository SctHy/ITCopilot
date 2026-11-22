"""模型出站边界：回合内映射、工具参数还原、用户可见文本不还原。

借鉴 Magpie 的双向替换设计，独立实现，仅支持当前使用的 Chat Completions 文本协议。
映射不落盘；未进入身份/会话作用域的后台调用使用一次性映射。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import json
import re
import secrets
import time

import httpx2

from it_copilot.agent.masking.redaction import REDACTED, known_secrets, sensitive_spans
from it_copilot.infra.monitor import metrics

_KEY = secrets.token_bytes(32)
_CURRENT: ContextVar[MaskingSession | None] = ContextVar("model_masking", default=None)
_TOKEN = re.compile(r"\{\{\s*([A-Z_]+_[a-f0-9]{24})\s*\}\}", re.I)
_SECRET_FIELD = re.compile(r"password|passwd|secret|api.?key|authorization|access.?token|private.?key|验证码|密码", re.I)


class MaskingError(RuntimeError):
    """不得重试为明文；异常消息不包含用户数据。"""


class MaskingSession:
    def __init__(self, scope: str | None = None):
        self.scope = scope or secrets.token_hex(16)
        self.values: dict[str, str] = {}
        self.reverse: dict[str, str] = {}
        self.counts: dict[str, int] = {}
        self._known_pattern = None
        self.findings = {}

    def remember(self, value: str, kind: str) -> str:
        if value in self.reverse:
            return self.reverse[value]
        if len(self.values) >= 4096 or len(value) > 65536:
            raise MaskingError("model masking capacity exceeded")
        digest = hmac.new(_KEY, json.dumps([self.scope, value]).encode(), hashlib.sha256).hexdigest()[:24]
        token = "{{" + kind + "_" + digest + "}}"
        if token in self.values and self.values[token] != value:
            raise MaskingError("model masking collision")
        self.values[token] = value
        self.reverse[value] = token
        self._known_pattern = None
        return token

    def learn(self, value, field=""):
        if isinstance(value, dict):
            for key, item in value.items():
                self.learn(item, key)
        elif isinstance(value, list):
            for item in value:
                self.learn(item, field)
        elif isinstance(value, str):
            if _SECRET_FIELD.search(field) and value and value != REDACTED:
                self.remember(value, "SECRET")
            rules = sensitive_spans(value)
            for start, end, kind in rules:
                self.remember(value[start:end], kind)
            for span in self.findings.get(value, ()):
                # Learn both sources. mask() resolves overlaps by earliest start / longest
                # span like Magpie apply(); a short rule must not suppress a full model span.
                self.remember(value[span.start:span.end], span.kind)
            # 工具返回值和参数常以 JSON 字符串承载，字段名也是检测依据。
            if value.lstrip().startswith(("{", "[")):
                try:
                    parsed = json.loads(value)
                except ValueError:
                    return
                self.learn(parsed)

    def mask(self, value):
        if isinstance(value, dict):
            return {key: self.mask(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.mask(item) for item in value]
        if not isinstance(value, str):
            return value
        # 先解析再序列化，避免引号/反斜杠转义使原值漏检。
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(self.mask(json.loads(value)), ensure_ascii=False)
            except ValueError:
                pass
        if not self.reverse:
            return value
        # 已知值扩展仅用于 >= 8 字符，短口令仍按当前文本的检测位置遮盖。
        # 避免 password=a 把所有普通英文中的 a 也替换掉。
        found = sensitive_spans(value)
        found.extend((s.start, s.end, s.kind) for s in self.findings.get(value, ()))
        if value in self.reverse:
            found = [(0, len(value), "")]
        if self._known_pattern is None:
            known = [v for v in self.reverse if len(v.encode()) >= 8]
            self._known_pattern = re.compile("|".join(re.escape(v) for v in sorted(known, key=len, reverse=True))) if known else False
        if self._known_pattern:
            found.extend((m.start(), m.end(), "") for m in self._known_pattern.finditer(value))
        chosen = []
        tokens = [m.span() for m in _TOKEN.finditer(value)]
        for a, b, _ in sorted(found, key=lambda item: (item[0], -(item[1] - item[0]))):
            if any(a < y and b > x for x, y in tokens) or (chosen and a < chosen[-1][1]):
                continue
            raw = value[a:b]
            if raw in self.reverse:
                chosen.append((a, b, self.reverse[raw]))
        for a, b, token in reversed(chosen):
            kind = token[2:-2].rsplit("_", 1)[0]
            self.counts[kind] = self.counts.get(kind, 0) + 1
            value = value[:a] + token + value[b:]
        return value

    def restore(self, text: str, *, tool: bool = False) -> str:
        def replace(match):
            token = "{{" + match[1].upper().rsplit("_", 1)[0] + "_" + match[1].rsplit("_", 1)[1].lower() + "}}"
            if not tool:
                return REDACTED
            if token not in self.values:
                raise MaskingError("unknown model placeholder in tool arguments")
            # tool arguments 是 JSON 文本，原值必须按字符串内容转义。
            return json.dumps(self.values[token], ensure_ascii=False)[1:-1]
        return _TOKEN.sub(replace, text)


@contextmanager
def masking_scope(owner: str, conversation: str):
    scope = json.dumps([owner, conversation], ensure_ascii=False)
    current = _CURRENT.get()
    if current is not None and current.scope == scope:
        yield current
        return
    session = MaskingSession(scope)
    token = _CURRENT.set(session)
    known_token = known_secrets.set(session.reverse)
    try:
        yield session
    finally:
        _CURRENT.reset(token)
        known_secrets.reset(known_token)
        session.values.clear()
        session.reverse.clear()
        session._known_pattern = None
        session.findings.clear()


def current_session() -> MaskingSession:
    return _CURRENT.get() or MaskingSession()


def _request_leaves(body: dict):
    """处理完整消息；协议名称/ID不替换，schema 只替换说明文字。

    当前业务仅支持文本；遇到图片/音频等不检查内容即放行会造成保护假象，因此拒绝。
    """
    if body.get("functions") is not None or body.get("function_call") is not None:
        raise MaskingError("unsupported functions/function_call; use tools/tool_choice")
    leaves = []
    for message in body.get("messages", []):
        if message.get("role") == "function":
            raise MaskingError("unsupported function message; use tool messages")
        if any(key in message for key in ("audio", "images")):
            raise MaskingError("model masking supports text input only")
        for key in ("content", "reasoning_content"):
            value = message.get(key)
            if isinstance(value, list):
                for part in value:
                    if part.get("type") != "text":
                        raise MaskingError("model masking supports text input only")
                    leaves.append((part, "text"))
            elif isinstance(value, str):
                leaves.append((message, key))
            elif value is not None:
                raise MaskingError("unsupported model message content")
        calls = message.get("tool_calls", [])
        if message.get("function_call") is not None:
            raise MaskingError("unsupported function_call; use tool_calls")
        for call in calls:
            leaves.append((call["function"], "arguments"))
    def schema(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "description" and isinstance(value, str):
                    leaves.append((node, key))
                elif isinstance(value, (dict, list)):
                    schema(value)
                elif isinstance(value, str) and sensitive_spans(value):
                    raise MaskingError("sensitive value in model protocol/schema metadata")
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, str) and sensitive_spans(value):
                    raise MaskingError("sensitive value in model schema enum")
                schema(value)
    schema(body.get("tools", []))
    schema(body.get("response_format", {}))
    for key, value in body.items():
        if key not in {"messages", "tools", "response_format"}:
            schema({key: value})
    return leaves


def _texts(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _texts(item)
    elif isinstance(value, list):
        for item in value:
            yield from _texts(item)
    elif isinstance(value, str):
        yield value
        if value.lstrip().startswith(("{", "[")):
            try:
                yield from _texts(json.loads(value))
            except ValueError:
                pass


async def prepare_local(value, session: MaskingSession):
    from it_copilot.agent.masking.local_pii import find_texts
    # 模型在工作线程中推理；仅完成后把结果应用到会话，取消不会继续改写会话。
    found = await find_texts(list(dict.fromkeys(_texts(value))))
    session.findings = found


async def prepare_request(body: dict, session: MaskingSession):
    leaves = _request_leaves(body)
    await prepare_local([obj.get(key) for obj, key in leaves], session)


def mask_request(body: dict, session: MaskingSession) -> dict:
    """规则与已准备的本地模型结果合并；调用者先 await prepare_request。"""
    body = json.loads(json.dumps(body))
    leaves = _request_leaves(body)
    # 两遍遍历：先发现，再替换，早于 password= 出现的裸值也能遮盖。
    for obj, key in leaves:
        session.learn(obj.get(key))
    for obj, key in leaves:
        obj[key] = session.mask(obj.get(key))
    return body


class _TextBuffer:
    def __init__(self, session, tool=False):
        self.session, self.tool, self.pending = session, tool, ""

    def feed(self, text, final=False):
        text = self.pending + text
        cut = len(text)
        if not final:
            start = text.rfind("{{")
            if start >= 0 and "}}" not in text[start:]:
                cut = start
            elif text.endswith("{"):
                cut -= 1
            if len(text) - cut > 128:
                raise MaskingError("invalid model placeholder fragment")
        self.pending = text[cut:]
        return self.session.restore(text[:cut], tool=self.tool)


def _restore_body(body, session):
    for choice in body.get("choices", []):
        msg = choice.get("message", {})
        for key in ("content", "reasoning_content", "refusal"):
            if isinstance(msg.get(key), str):
                msg[key] = session.restore(msg[key])
        for call in msg.get("tool_calls", []):
            fn = call["function"]
            fn["arguments"] = session.restore(fn["arguments"], tool=True)
        if msg.get("function_call") is not None:
            raise MaskingError("unsupported function_call; use tool_calls")
    return body


class _ResponseStream(httpx2.AsyncByteStream):
    def __init__(self, response, session):
        self.response, self.session = response, session
        self.buffers = {}

    async def __aiter__(self):
        # aiter_lines handles UTF-8 and CRLF across arbitrary network chunk boundaries.
        event = []
        async for line in self.response.aiter_lines():
            if line:
                event.append(line)
                continue
            if event:
                yield self.event(event)
                event = []
        if event:
            yield self.event(event)
        if any(b.pending for b in self.buffers.values()):
            raise MaskingError("incomplete model placeholder stream")

    def event(self, lines):
        data = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
        if not data or data == "[DONE]":
            if data and any(b.pending for b in self.buffers.values()):
                raise MaskingError("incomplete model placeholder stream")
            return ("\n".join(lines) + "\n\n").encode()
        body = json.loads(data)
        for choice in body.get("choices", []):
            index = choice.get("index", 0)
            delta = choice.setdefault("delta", {})
            for key in ("content", "reasoning_content", "refusal"):
                if isinstance(delta.get(key), str):
                    buf = self.buffers.setdefault((index, key), _TextBuffer(self.session))
                    delta[key] = buf.feed(delta[key])
            for call in delta.get("tool_calls", []):
                fn = call.get("function", {})
                if "arguments" in fn:
                    buf = self.buffers.setdefault((index, "tool", call["index"]), _TextBuffer(self.session, tool=True))
                    fn["arguments"] = buf.feed(fn["arguments"])
            if delta.get("function_call") is not None:
                raise MaskingError("unsupported function_call; use tool_calls")
            if choice.get("finish_reason"):
                for k, buf in self.buffers.items():
                    if k[0] != index or not buf.pending:
                        continue
                    if buf.pending != "{":
                        raise MaskingError("incomplete model placeholder stream")
                    tail = buf.feed("", final=True)
                    if k[1] == "tool":
                        delta.setdefault("tool_calls", []).append({"index": k[2], "function": {"arguments": tail}})
                    else:
                        delta[k[1]] = delta.get(k[1], "") + tail
        return ("data: " + json.dumps(body, ensure_ascii=False) + "\n\n").encode()

    async def aclose(self):
        await self.response.aclose()


class MaskingTransport(httpx2.AsyncBaseTransport):
    """在 OpenAI SDK 序列化之后检查实际请求；API 鉴权头正常交给指定服务商。"""
    def __init__(self, wrapped=None):
        self.wrapped = wrapped
        # 由内层客户端负责环境代理/NO_PROXY；把脱敏 transport 直接用作
        # AsyncClient.transport 会关闭 httpx 的自动代理挂载。
        self.client = None if wrapped is not None else httpx2.AsyncClient(
            limits=httpx2.Limits(max_connections=1000, max_keepalive_connections=100))

    async def handle_async_request(self, request):
        started = time.perf_counter()
        session = current_session()
        before = dict(session.counts)
        try:
            if not request.url.path.rstrip("/").endswith("/chat/completions"):
                raise MaskingError("unsupported model endpoint")
            original = json.loads(await request.aread())
            await prepare_request(original, session)
            body = mask_request(original, session)
            headers = dict(request.headers)
            headers.pop("content-length", None)
            headers["accept-encoding"] = "identity"
            outbound = httpx2.Request(request.method, request.url, headers=headers, json=body, extensions=request.extensions)
        except Exception:
            metrics.MODEL_MASK_REQUESTS.labels(outcome="blocked").inc()
            raise MaskingError("model request could not be masked; nothing sent") from None
        finally:
            metrics.MODEL_MASK_LATENCY.observe(time.perf_counter() - started)
        metrics.MODEL_MASK_REQUESTS.labels(outcome="sent").inc()
        for kind, count in session.counts.items():
            metrics.MODEL_MASK_VALUES.labels(kind=kind).inc(count - before.get(kind, 0))
        # 响应只恢复本次出站时已知的值；后台任务不受父回合作用域结束清理影响。
        reply_session = MaskingSession(session.scope)
        reply_session.values = dict(session.values)
        response = (await self.wrapped.handle_async_request(outbound) if self.wrapped is not None
                    else await self.client.send(outbound, stream=True))
        headers = dict(response.headers)
        headers.pop("content-length", None)
        headers.pop("content-encoding", None)
        if response.status_code >= 400:
            await response.aclose()
            # 厂商错误可能回显请求片段；只保留状态，不写入日志/异常正文。
            return httpx2.Response(response.status_code, headers=headers, json={"error": {"message": "model provider rejected request", "type": "provider_error"}})
        if "text/event-stream" in response.headers.get("content-type", ""):
            return httpx2.Response(response.status_code, headers=headers, stream=_ResponseStream(response, reply_session))
        try:
            await response.aread()
            return httpx2.Response(response.status_code, headers=headers, json=_restore_body(response.json(), reply_session))
        finally:
            await response.aclose()

    async def aclose(self):
        await (self.wrapped.aclose() if self.wrapped is not None else self.client.aclose())
