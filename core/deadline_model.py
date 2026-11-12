"""Apply the remaining turn budget to every request, including tool-loop requests."""
from contextlib import asynccontextmanager
import asyncio
import time

from pydantic_ai.exceptions import ModelRequestTimeout
from pydantic_ai.models.wrapper import WrapperModel


class TurnDeadlineExceeded(RuntimeError):
    pass


class DeadlineModel(WrapperModel):
    def __init__(self, model, *, deadline, request_timeout_s):
        super().__init__(model)
        self.deadline, self.request_timeout_s = deadline, request_timeout_s

    def _bounded_settings(self, settings):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TurnDeadlineExceeded("Agent turn deadline exceeded")
        timeout = min(self.request_timeout_s, remaining)
        return {**settings, "timeout": timeout, "request_timeout": timeout}

    async def request(self, messages, model_settings, model_request_parameters):
        settings = self._bounded_settings(model_settings)
        budget = asyncio.timeout(settings["request_timeout"])
        try:
            async with budget:
                return await self.wrapped.request(messages, settings, model_request_parameters)
        except TimeoutError as exc:
            if not budget.expired():
                raise
            raise ModelRequestTimeout(self.model_name, settings["request_timeout"]) from exc

    @asynccontextmanager
    async def request_stream(self, messages, model_settings, model_request_parameters, run_context=None):
        settings = self._bounded_settings(model_settings)
        budget = asyncio.timeout(settings["request_timeout"])
        try:
            async with budget:
                async with self.wrapped.request_stream(messages, settings, model_request_parameters, run_context) as stream:
                    yield stream
        except TimeoutError as exc:
            if not budget.expired():
                raise
            raise ModelRequestTimeout(self.model_name, settings["request_timeout"]) from exc
