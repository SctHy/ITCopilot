"""Token 审计：拦截每次 chat.completions 调用，按组成部分统计请求字符数，并记录服务端返回的真实用量。

用法（评测脚本内）：
    with TokenAudit() as audit:
        ...  # 跑用例
    report = audit.summary()

组成部分（按字符数）：system、tools（工具定义 JSON）、history（本轮用户消息之前的多轮对话）、
turn（本轮用户消息，再按 [背景信息] / [知识库检索结果] 等小节拆分）、in_turn（本轮内的工具调用与返回、纠正提示）。
用量取 DeepSeek 的 usage：prompt_cache_hit_tokens / prompt_cache_miss_tokens / completion_tokens /
completion_tokens_details.reasoning_tokens。费用按 deepseek-flash 价目（美元 / 百万 token）。
"""
from __future__ import annotations

import json
import re
import statistics
from typing import Any, Dict, List
from unittest.mock import patch

# deepseek-flash（api-docs.deepseek.com/quick_start/pricing，2026-11 查询）；高峰时段为非高峰的 2 倍。
PRICES = {"off_peak": {"hit": 0.003, "miss": 0.15, "output": 0.6},
          "peak": {"hit": 0.006, "miss": 0.3, "output": 1.2}}
SECTION = re.compile(r"^\[([^\]\n\[{\"]{1,24})\]", re.M)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
    return "" if content is None else str(content)


def _role(kwargs: Dict[str, Any]) -> str:
    names = {t.get("function", {}).get("name") for t in kwargs.get("tools") or []}
    if "classify_it_request" in names or any(n and n.startswith("final_result") for n in names) and len(names) <= 2:
        return "intent"
    if names & {"search_knowledge_base", "create_ticket", "request_access", "lookup_asset"}:
        return "agent"
    return "other"


def _composition(kwargs: Dict[str, Any]) -> Dict[str, int]:
    messages = kwargs.get("messages") or []
    out = {"system": 0, "tools": len(json.dumps(kwargs.get("tools") or [], ensure_ascii=False)), "history": 0, "turn": 0, "in_turn": 0}
    users = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    # 纠正提示也是 user 消息；本轮消息取最后一条不以 [系统核对] 开头的 user 消息。
    turn_index = next((i for i in reversed(users) if not _text(messages[i].get("content")).startswith("[系统核对]")), users[-1] if users else -1)
    sections: Dict[str, int] = {}
    for i, m in enumerate(messages):
        text = _text(m.get("content")) + "".join(json.dumps(c, ensure_ascii=False) for c in m.get("tool_calls") or [])
        if m.get("role") in ("system", "developer"):
            out["system"] += len(text)
        elif i < turn_index:
            out["history"] += len(text)
        elif i == turn_index:
            out["turn"] += len(text)
            heads = list(SECTION.finditer(text))
            for k, h in enumerate(heads):
                end = heads[k + 1].start() if k + 1 < len(heads) else len(text)
                sections[h.group(1)] = sections.get(h.group(1), 0) + end - h.start()
            if not heads:
                sections["用户消息"] = len(text)
        else:
            out["in_turn"] += len(text)
    out.update({f"turn:{k}": v for k, v in sections.items()})
    return out


def _usage(response: Any) -> Dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
    details = data.get("completion_tokens_details") or {}
    prompt = int(data.get("prompt_tokens") or 0)
    hit = int(data.get("prompt_cache_hit_tokens") or (data.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    return {"prompt": prompt, "hit": hit, "miss": int(data.get("prompt_cache_miss_tokens") or prompt - hit),
            "output": int(data.get("completion_tokens") or 0), "reasoning": int(details.get("reasoning_tokens") or 0)}


def cost(usage: Dict[str, int], tier: str = "off_peak") -> float:
    p = PRICES[tier]
    return (usage.get("hit", 0) * p["hit"] + usage.get("miss", 0) * p["miss"] + usage.get("output", 0) * p["output"]) / 1e6


class TokenAudit:
    def __init__(self):
        self.records: List[Dict[str, Any]] = []
        self._patch = None

    def __enter__(self):
        from openai.resources.chat.completions import AsyncCompletions
        original = AsyncCompletions.create
        audit = self

        async def create(client_self, *args, **kwargs):
            response = await original(client_self, *args, **kwargs)
            if kwargs.get("stream"):
                # 流式响应的 usage 在最后一个 chunk；包一层迭代器在结束时记录。
                return _StreamTap(response, kwargs, audit)
            audit.records.append({"role": _role(kwargs), "chars": _composition(kwargs), "usage": _usage(response),
                                  "thinking": (kwargs.get("extra_body") or {}).get("thinking"), "effort": kwargs.get("reasoning_effort")})
            return response

        self._patch = patch.object(AsyncCompletions, "create", create)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()

    def summary(self) -> Dict[str, Any]:
        return summarize(self.records)


class _StreamTap:
    def __init__(self, stream, kwargs, audit):
        self._stream, self._kwargs, self._audit, self._usage = stream, kwargs, audit, None

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        async for chunk in self._stream:
            if getattr(chunk, "usage", None) is not None:
                self._usage = chunk
            yield chunk
        self._audit.records.append({"role": _role(self._kwargs), "chars": _composition(self._kwargs),
                                    "usage": _usage(self._usage) if self._usage else {},
                                    "thinking": (self._kwargs.get("extra_body") or {}).get("thinking"),
                                    "effort": self._kwargs.get("reasoning_effort")})

    def __getattr__(self, name):
        return getattr(self._stream, name)

    async def __aenter__(self):
        await self._stream.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._stream.__aexit__(*exc)


def summarize(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_role: Dict[str, Any] = {}
    for role in sorted({r["role"] for r in records}):
        rows = [r for r in records if r["role"] == role]
        usage = {k: sum(r["usage"].get(k, 0) for r in rows) for k in ("prompt", "hit", "miss", "output", "reasoning")}
        keys = sorted({k for r in rows for k in r["chars"]})
        by_role[role] = {
            "calls": len(rows), "usage": usage,
            "cache_hit_rate": round(usage["hit"] / usage["prompt"], 3) if usage["prompt"] else 0,
            "cost_usd_off_peak": round(cost(usage), 6), "cost_usd_peak": round(cost(usage, "peak"), 6),
            "chars_mean": {k: round(statistics.mean(r["chars"].get(k, 0) for r in rows)) for k in keys},
        }
    total = {k: sum(r["usage"].get(k, 0) for r in records) for k in ("prompt", "hit", "miss", "output", "reasoning")}
    return {"calls": len(records), "usage": total, "cost_usd_off_peak": round(cost(total), 6),
            "cost_usd_peak": round(cost(total, "peak"), 6), "by_role": by_role}
