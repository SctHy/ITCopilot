"""内置工具的执行属性与参数约束。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

# 处理函数签名：(it_copilot.agent.types.Request, 参数) -> 结果；这里不引用 Request，避免反向依赖 agents。
AgentToolHandler = Callable[[Any, Dict[str, Any]], Union[Any, Awaitable[Any]]]
ToolSet = Dict[str, "AgentToolSpec"]


@dataclass(frozen=True)
class AgentToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: AgentToolHandler
    retry_safe: bool = True
    timeout_s: Optional[float] = None
    # 处理函数内部已有熔断、重试与兜底（如经 ToolManager 的知识检索），执行层只保留总时限。
    self_governed: bool = False


def make_tool(name: str, description: str, properties: Dict[str, Any], handler: AgentToolHandler, required: Optional[List[str]] = None, *, retry_safe: bool = True, self_governed: bool = False) -> AgentToolSpec:
    schema = {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}
    return AgentToolSpec(name, description, schema, handler, retry_safe, self_governed=self_governed)
