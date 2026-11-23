"""PydanticAI 模型工厂与单轮文本补全。"""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Any, Optional

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from urllib.parse import urlsplit

from openai import DefaultAsyncHttpxClient
from servicedesk_agent.agent.masking.model_masking import MaskingTransport

from pydantic_ai.direct import model_request
from pydantic_ai.exceptions import ModelConnectionError, ModelHTTPError, ModelRequestTimeout, ModelServerError, ModelUnavailableError
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

DEFAULT_MODEL = "deepseek-flash"


def build_model(api_key: str | None = None, base_url: Optional[str] = None, model: str = "", settings: Optional[ModelSettings] = None, *, api="chat_completions", anthropic_breakpoints=False) -> Model:
    """供应商凭据与地址由路由解析，SDK 客户端仅使用该模型的配置。"""
    responses = api == "responses"
    if api_key is None:
        # Direct callers use the provider's namespace. The router passes an explicit
        # string (including the empty string) after resolving the model configuration.
        api_key = os.getenv("OPENAI_GPT_API_KEY" if responses else "OPENAI_API_KEY", "")
        base_url = base_url or os.getenv("OPENAI_GPT_BASE_URL" if responses else "OPENAI_BASE_URL")
    name = model or ("gpt-6.2-sol" if responses else os.getenv("OPENAI_MODEL") or DEFAULT_MODEL)
    # 本地无鉴权服务不需要 key；空 key 时给占位值，避免 SDK 在构造期报错。
    endpoint = base_url or ("https://api.deepseek.com" if name == DEFAULT_MODEL else "https://api.openai.com/v1")
    missing_key = not api_key and urlsplit(endpoint).hostname in {"api.openai.com", "api.deepseek.com"}
    transport = MaskingTransport(instructions_as_input=responses, anthropic_breakpoints=anthropic_breakpoints, missing_key=missing_key)
    provider = OpenAIProvider(base_url=endpoint, api_key=api_key or "unconfigured",
                              http_client=DefaultAsyncHttpxClient(transport=transport))
    model_class = OpenAIResponsesModel if api == "responses" else OpenAIChatModel
    return model_class(name, provider=provider, settings=settings)


async def close_model(model: Any) -> None:
    client = getattr(model, "client", None)
    close = getattr(client, "close", None)
    if close is not None:
        await close()


def is_model_unavailable(exc: BaseException) -> bool:
    """模型服务不可达：连接失败、超时或 5xx。参数/鉴权类错误不算。"""
    if isinstance(exc, (ModelConnectionError, ModelRequestTimeout, ModelServerError, ModelUnavailableError, TimeoutError)):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(exc, ModelHTTPError) or status is not None:
        try:
            return int(status) >= 500
        except (TypeError, ValueError):
            return False
    name = type(exc).__name__.lower()
    return any(token in name for token in ("apiconnection", "connecterror", "timeouterror", "timeoutexception", "readtimeout", "connecttimeout"))


@dataclass
class TextCompletion:
    text: str
    finish_reason: Optional[str] = None

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


async def complete_text(model: Model, prompt: str, *, history=None, prefix=None, model_settings=None, max_tokens: int, temperature: float = 0.0, request_timeout: Optional[float] = None, max_retries: Optional[int] = None) -> TextCompletion:
    """单次纯文本补全；可复用主会话前缀，工具调用永远不执行。"""
    settings = ModelSettings(**(model_settings or {}))
    settings.update(max_tokens=max_tokens, temperature=temperature)
    if request_timeout is not None:
        settings["timeout"] = request_timeout
    if max_retries is not None:
        settings["max_retries"] = max_retries
    messages = list(history or ())
    instructions, tools = prefix or (None, [])
    messages.append(ModelRequest.user_text_prompt(prompt, instructions=instructions))
    response = await model_request(model, messages, model_settings=settings,
                                   model_request_parameters=ModelRequestParameters(function_tools=tools))
    return TextCompletion("" if response.tool_calls else response.text or "", response.finish_reason)
