"""
Anthropic Messages → OpenAI Chat Completions 协议转换网关。

为什么存在：
  IT-Copilot 默认通过 PydanticAI 的 AnthropicModel 调用 Anthropic Messages 协议
  （system 字段、content blocks、tool_use / tool_result、tool_choice）。而手头只有 OpenAI Chat Completions 兼容接口
  （user/assistant/system/tool 消息、function tools、tool_calls）。
  本网关在本地暴露一个 Anthropic Messages 兼容端点 `/v1/messages`，
  无状态地把请求翻译成 OpenAI 格式、转发给上游，再把响应翻译回 Anthropic 格式。
  这样 IT-Copilot 业务代码零改动即可使用 OpenAI 兼容模型。

配置（环境变量，见 gateway/.env.gateway.example）：
  OPENAI_BASE_URL   OpenAI 兼容服务的基础地址（含 /v1），例如
                    https://api.openai.com/v1 或 http://127.0.0.1:11434/v1
  OPENAI_API_KEY    上游服务的 Key（本地无鉴权服务可填任意非空值）
  OPENAI_MODEL      可选。非空时强制覆盖上游模型名；为空时透传请求里的 model。
  GATEWAY_PORT      监听端口，默认 8082
  OPENAI_USE_MAX_COMPLETION_TOKENS
                    设为 1 时使用 max_completion_tokens（o1/o3 等新模型），
                    默认使用 max_tokens。
  GATEWAY_TIMEOUT_S 上游请求超时，默认 120 秒。

运行：
  python gateway/anthropic_to_openai.py
  （或 uvicorn gateway.anthropic_to_openai:app --port 8082）
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("anthropic_to_openai")
logging.basicConfig(level=os.getenv("GATEWAY_LOG_LEVEL", "INFO"))

app = FastAPI(title="Anthropic→OpenAI Gateway", version="1.0.0")


# ──────────────────────────────────────────────────────────────────────────
# 配置
# ──────────────────────────────────────────────────────────────────────────

def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _chat_completions_url() -> str:
    base = _env("OPENAI_BASE_URL")
    if not base:
        raise RuntimeError("未配置 OPENAI_BASE_URL")
    base = base.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _resolve_model(incoming: str) -> str:
    forced = _env("OPENAI_MODEL")
    return forced or incoming or ""


# ──────────────────────────────────────────────────────────────────────────
# 请求翻译：Anthropic Messages → OpenAI Chat Completions
# ──────────────────────────────────────────────────────────────────────────

def _system_text(system: Any) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: List[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type", "text") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(p for p in parts if p)
    return str(system)


def _tool_result_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type", "text") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False)


def convert_messages(
    messages: List[Dict[str, Any]],
    system: Any,
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []

    sys_text = _system_text(system)
    if sys_text.strip():
        out.append({"role": "system", "content": sys_text})

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            # 空内容或异常类型，兜底为空字符串
            out.append({"role": role, "content": content if content is not None else ""})
            continue

        if role == "assistant":
            text_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []
            for block in content:
                btype = block.get("type", "text")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    raw_input = block.get("input", {})
                    if not isinstance(raw_input, (dict, list)):
                        raw_input = {"_value": raw_input}
                    tool_calls.append({
                        "id": block.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                        "type": "function",
                        "function": {
                            "name": block.get("name"),
                            "arguments": json.dumps(raw_input, ensure_ascii=False),
                        },
                    })
            assistant_msg: Dict[str, Any] = {"role": "assistant"}
            joined_text = "\n".join(p for p in text_parts if p)
            # 用空字符串而非 null，兼容更严格的 OpenAI 兼容服务
            assistant_msg["content"] = joined_text
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            out.append(assistant_msg)
            continue

        # role == user：文本块聚合成 user 消息，tool_result 块转成 tool 消息
        text_buf: List[str] = []
        for block in content:
            btype = block.get("type", "text")
            if btype == "text":
                text_buf.append(block.get("text", ""))
            elif btype == "tool_result":
                _flush_text(out, text_buf)
                out.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _tool_result_content(block.get("content")),
                })
        _flush_text(out, text_buf)

    return out


def _flush_text(out: List[Dict[str, Any]], text_buf: List[str]) -> None:
    """把累积的 user 文本块合并成一条 user 消息（空白内容丢弃）。"""
    joined = "\n".join(p for p in text_buf if p)
    if joined.strip():
        out.append({"role": "user", "content": joined})
    text_buf.clear()


def convert_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for tool in tools or []:
        parameters = tool.get("input_schema") or {"type": "object", "properties": {}}
        result.append({
            "type": "function",
            "function": {
                "name": tool.get("name"),
                "description": tool.get("description", ""),
                "parameters": parameters,
            },
        })
    return result


def convert_tool_choice(choice: Any) -> Any:
    """Anthropic tool_choice → OpenAI tool_choice；结构化输出依赖 any/tool 被如实转发。"""
    ctype = choice.get("type") if isinstance(choice, dict) else None
    if ctype == "any":
        return "required"
    if ctype == "tool" and choice.get("name"):
        return {"type": "function", "function": {"name": choice["name"]}}
    if ctype == "none":
        return "none"
    return "auto"


# ──────────────────────────────────────────────────────────────────────────
# 响应翻译：OpenAI Chat Completions → Anthropic Messages
# ──────────────────────────────────────────────────────────────────────────

_FINISH_REASON_MAP = {
    "stop": "end_turn",
    "end_turn": "end_turn",
    "tool_calls": "tool_use",
    "tool_use": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "max_tokens": "max_tokens",
    "content_filter": "end_turn",
}


def _parse_tool_arguments(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"_value": parsed}
    except Exception:
        # 上游返回了非合法 JSON 的 arguments，保留原文以便上层排查
        return {"_raw_arguments": raw}


def convert_response(oai: Dict[str, Any], model: str) -> Dict[str, Any]:
    choices = oai.get("choices") or []
    if not choices:
        raise ValueError("上游响应缺少 choices")
    choice = choices[0]
    message = choice.get("message") or {}

    content_blocks: List[Dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        content_blocks.append({"type": "text", "text": text})

    for tc in (message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
            "name": fn.get("name"),
            "input": _parse_tool_arguments(fn.get("arguments")),
        })

    if not content_blocks:
        # 上游可能返回空内容，避免下游解析失败
        content_blocks.append({"type": "text", "text": ""})

    finish = choice.get("finish_reason") or "stop"
    stop_reason = _FINISH_REASON_MAP.get(finish, "end_turn")

    usage = oai.get("usage") or {}
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# 错误翻译
# ──────────────────────────────────────────────────────────────────────────

def _error_body(status: int, message: str) -> Tuple[int, Dict[str, Any]]:
    if status in (401,):
        etype = "authentication_error"
    elif status in (403,):
        etype = "permission_error"
    elif status in (404,):
        etype = "not_found_error"
    elif status in (429,):
        etype = "rate_limit_error"
    elif status in (400, 422):
        etype = "invalid_request_error"
    elif status in (503,):
        etype = "overloaded_error"
    else:
        etype = "api_error"
    return status, {"type": "error", "error": {"type": etype, "message": message}}


# ──────────────────────────────────────────────────────────────────────────
# 路由
# ──────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def gateway_health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "openai_base_url": _env("OPENAI_BASE_URL"),
        "openai_model": _env("OPENAI_MODEL"),
        "key_configured": bool(_env("OPENAI_API_KEY")),
    }


@app.post("/v1/messages/count_tokens")
async def count_tokens() -> Dict[str, Any]:
    # 兜底：IT-Copilot 未使用，但避免客户端误调用报错
    return {"input_tokens": 0}


@app.post("/v1/messages")
async def messages(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        status, body = _error_body(400, "请求体不是合法 JSON")
        return JSONResponse(status_code=status, content=body)

    try:
        upstream_url = _chat_completions_url()
        upstream_key = _env("OPENAI_API_KEY")
        if not upstream_key:
            raise RuntimeError("未配置 OPENAI_API_KEY")

        model = _resolve_model(str(payload.get("model", "")))
        openai_messages = convert_messages(
            payload.get("messages") or [],
            payload.get("system"),
        )

        body: Dict[str, Any] = {
            "model": model,
            "messages": openai_messages,
        }
        # Some compatible providers enable reasoning by default and reject forced
        # tool_choice. Explicit opt-in setting; never silently weaken ToolOutput.
        thinking_type = _env("OPENAI_THINKING_TYPE")
        if thinking_type:
            if thinking_type not in {"enabled", "disabled"}:
                raise ValueError("OPENAI_THINKING_TYPE must be enabled or disabled")
            body["thinking"] = {"type": thinking_type}

        # 采样参数
        if payload.get("temperature") is not None:
            body["temperature"] = payload["temperature"]
        if payload.get("top_p") is not None:
            body["top_p"] = payload["top_p"]
        # top_k 是 Anthropic 专有，OpenAI 不支持，丢弃
        if payload.get("stop_sequences"):
            body["stop"] = payload["stop_sequences"]

        # 输出长度
        max_tokens = payload.get("max_tokens")
        if _env("OPENAI_USE_MAX_COMPLETION_TOKENS") == "1":
            if max_tokens:
                body["max_completion_tokens"] = max_tokens
        else:
            body["max_tokens"] = max_tokens or 1024

        tools = convert_tools(payload.get("tools"))
        if tools:
            body["tools"] = tools
            body["tool_choice"] = convert_tool_choice(payload.get("tool_choice"))
            if (payload.get("tool_choice") or {}).get("disable_parallel_tool_use"):
                body["parallel_tool_calls"] = False

        headers = {
            "Authorization": f"Bearer {upstream_key}",
            "Content-Type": "application/json",
        }
        timeout = float(_env("GATEWAY_TIMEOUT_S", "120") or 120)

        logger.info("→ upstream model=%s messages=%d tools=%d",
                    model, len(openai_messages), len(tools))

        async with httpx.AsyncClient(timeout=timeout) as client:
            upstream = await client.post(upstream_url, headers=headers, json=body)

        if upstream.status_code >= 400:
            logger.warning("upstream error %s: %s", upstream.status_code, upstream.text[:500])
            status, err = _error_body(upstream.status_code, upstream.text[:1000])
            return JSONResponse(status_code=status, content=err)

        oai = upstream.json()
        anthropic_resp = convert_response(oai, model)
        return JSONResponse(status_code=200, content=anthropic_resp)

    except httpx.ConnectError as ex:
        status, err = _error_body(502, f"无法连接上游模型服务: {ex}")
        return JSONResponse(status_code=status, content=err)
    except httpx.TimeoutException:
        status, err = _error_body(504, "上游模型服务请求超时")
        return JSONResponse(status_code=status, content=err)
    except Exception as ex:
        logger.exception("gateway error")
        status, err = _error_body(500, f"网关内部错误: {ex}")
        return JSONResponse(status_code=status, content=err)


if __name__ == "__main__":
    import uvicorn

    port = int(_env("GATEWAY_PORT", "8082") or 8082)
    uvicorn.run(app, host="127.0.0.1", port=port)
