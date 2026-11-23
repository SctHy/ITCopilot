"""Budget the actual provider request, including tool-loop and correction requests."""
import hashlib
import json
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict, is_dataclass

from pydantic_ai.exceptions import ModelContextWindowExceededError, ModelHTTPError
from pydantic_ai.models.wrapper import WrapperModel


class ContextTooLong(ValueError):
    pass


def text_tokens(text):
    return (len(text.encode()) + 2) // 3


def serialized(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def message_text(message):
    parts = []
    for part in message.parts:
        content = getattr(part, "content", getattr(part, "args", ""))
        parts.append(serialized([part.part_kind, getattr(part, "tool_name", ""), getattr(part, "tool_call_id", ""), content]))
    return "\n".join(parts)


def prefix_digest(static, texts):
    digest = hashlib.sha256()
    for value in (static, *texts):
        digest.update(value.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def overflow(exc):
    return isinstance(exc, ModelContextWindowExceededError) or exc.status_code == 400 and any(marker in str(exc).lower() for marker in
        ("context_length_exceeded", "maximum context length", "context window exceeded"))


class BudgetedModel(WrapperModel):
    def __init__(self, model, req, router, selection, turn):
        super().__init__(model)
        self.req, self.selection, self.turn = req, selection, turn
        self.trigger, self.keep = router.compaction_budget(selection.name)
        self.window = router.config.models[selection.name].context_window

    async def prepare(self, messages, params, force=False):
        req = self.req
        history_len = len(req.model_history or ())
        tail = messages[history_len:]
        history = self.turn.replacement if self.turn.replacement is not None else messages[:history_len]
        static = next((m.instructions for m in reversed(messages) if getattr(m, "instructions", None)), "")
        static += serialized([asdict(tool) if is_dataclass(tool) else str(tool) for tool in params.function_tools])
        prefix = [message_text(m) for m in history]
        suffix = [message_text(m) for m in tail]
        added = sum(map(text_tokens, suffix))
        if text_tokens(req.message) >= self.window:
            raise ContextTooLong
        state = req.conversation_state or {}
        measured = state.get("last_context_tokens")
        if measured and state.get("last_context_model") == self.selection.name and state.get("last_context_prefix") == prefix_digest(static, prefix):
            size = measured + added
        else:
            size = text_tokens(static) + sum(map(text_tokens, prefix)) + added
        if force or size >= self.trigger:
            if req.compact_history and history:
                replacement = await req.compact_history(self.selection, min(self.keep, max(0, self.trigger - added - text_tokens(static))))
                if replacement is not None:
                    self.turn.replacement = history = replacement
                    prefix = [message_text(m) for m in history]
                elif force:
                    raise ContextTooLong
            elif force:
                raise ContextTooLong
        if text_tokens(static) + sum(map(text_tokens, prefix)) + added >= self.window and (force or size >= self.trigger):
            raise ContextTooLong
        self.sent_prefix = (static, prefix + suffix)
        return [*history, *tail]

    def record_response(self, response):
        self.turn.last_context_tokens = response.usage.input_tokens + response.usage.output_tokens
        if self.req.conversation_state is not None:
            static, sent = self.sent_prefix
            self.req.conversation_state.update(
                last_context_tokens=self.turn.last_context_tokens,
                last_context_model=self.selection.name,
                last_context_prefix=prefix_digest(static, [*sent, message_text(response)]),
            )

    def retry(self, exc):
        if not overflow(exc):
            return False
        if self.turn.retried or self.req.first_token_ms is not None:
            raise ContextTooLong from exc
        self.turn.retried = True
        return True

    async def request(self, messages, model_settings, model_request_parameters):
        for force in (False, True):
            prepared = await self.prepare(messages, model_request_parameters, force)
            try:
                response = await self.wrapped.request(prepared, model_settings, model_request_parameters)
                self.record_response(response)
                return response
            except (ModelContextWindowExceededError, ModelHTTPError) as exc:
                if not self.retry(exc):
                    raise

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        async with AsyncExitStack() as stack:
            for force in (False, True):
                prepared = await self.prepare(messages, model_request_parameters, force)
                try:
                    stream = await stack.enter_async_context(self.wrapped.request_stream(
                        prepared, model_settings, model_request_parameters, run_context))
                    break
                except (ModelContextWindowExceededError, ModelHTTPError) as exc:
                    if not self.retry(exc):
                        raise
            yield stream
            self.record_response(stream.get())
