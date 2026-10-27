"""PydanticAI 模型工厂与单轮调用辅助。

所有 LLM 调用统一经由 PydanticAI 的 Model 抽象：
  - 默认 provider=anthropic：Anthropic Messages 协议，指向 ANTHROPIC_BASE_URL（默认 8082 协议网关）；
  - provider=openai：直连 OpenAI-compatible 服务（OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL），可绕过网关。
"""
from __future__ import annotations
import os
from dataclasses import dataclass
from typing import Any, Optional

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

from pydantic_ai.direct import model_request
from pydantic_ai.exceptions import ModelConnectionError, ModelHTTPError, ModelRequestTimeout, ModelServerError, ModelUnavailableError
from pydantic_ai.messages import ModelRequest
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

DEFAULT_MODEL = "deepseek-chat"


def llm_provider() -> str:
    return (os.getenv("IT_COPILOT_LLM_PROVIDER") or "anthropic").strip().lower()


def build_model(api_key: str = "", base_url: Optional[str] = None, model: str = "") -> Model:
    """按 IT_COPILOT_LLM_PROVIDER 构造 PydanticAI Model；构造过程不发起网络请求。"""
    if llm_provider() == "openai":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider
        name = os.getenv("OPENAI_MODEL") or model or DEFAULT_MODEL
        provider = OpenAIProvider(base_url=os.getenv("OPENAI_BASE_URL") or None, api_key=os.getenv("OPENAI_API_KEY") or api_key or "sk-local")
        return OpenAIChatModel(name, provider=provider)
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider
    name = model or os.getenv("ANTHROPIC_MODEL") or DEFAULT_MODEL
    # 网关不校验入站 key；空 key 时给占位值，避免 SDK 在构造期报错。
    provider = AnthropicProvider(api_key=api_key or os.getenv("ANTHROPIC_API_KEY") or "sk-local", base_url=base_url or None)
    return AnthropicModel(name, provider=provider)


def model_base_url(model: Any) -> str:
    try: return str(getattr(model, "base_url", "") or "")
    except Exception: return ""


async def close_model(model: Any) -> None:
    client = getattr(model, "client", None)
    close = getattr(client, "close", None)
    if close is not None:
        await close()


def is_gateway_failure(exc: BaseException) -> bool:
    """模型网关/上游不可达：连接失败、超时或 5xx。参数/鉴权类错误不算。"""
    if isinstance(exc, (ModelConnectionError, ModelRequestTimeout, ModelServerError, ModelUnavailableError, TimeoutError)):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(exc, ModelHTTPError) or status is not None:
        try: return int(status) >= 500
        except (TypeError, ValueError): return False
    name = type(exc).__name__.lower()
    return any(token in name for token in ("apiconnection", "connecterror", "timeouterror", "timeoutexception", "readtimeout", "connecttimeout"))


@dataclass
class TextCompletion:
    text: str
    finish_reason: Optional[str] = None

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


async def complete_text(model: Model, prompt: str, *, max_tokens: int, temperature: float = 0.0, timeout: Optional[float] = None, max_retries: Optional[int] = None) -> TextCompletion:
    """单轮纯文本补全（无工具、无重试循环），用于摘要、评测裁判等场景。"""
    settings = ModelSettings(max_tokens=max_tokens, temperature=temperature)
    if timeout is not None: settings["request_timeout"] = timeout
    if max_retries is not None: settings["max_retries"] = max_retries
    response = await model_request(model, [ModelRequest.user_text_prompt(prompt)], model_settings=settings)
    return TextCompletion(response.text or "", response.finish_reason)
