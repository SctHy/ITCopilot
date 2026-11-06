"""Agent 工具的声明：名称、JSON Schema、处理函数与执行属性。

放在 tooling 层：本地工具（agents/tools.py）与外部 MCP 工具（tooling/mcp_client.py）都产出它，
统一执行层（tooling/executor.py）消费它。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple, Union

# 处理函数签名：(agents.types.Request, 参数) -> 结果；这里不引用 Request，避免反向依赖 agents。
AgentToolHandler = Callable[[Any, Dict[str, Any]], Union[Any, Awaitable[Any]]]
ToolSet = Dict[str, "AgentToolSpec"]


@dataclass(frozen=True)
class AgentToolSpec:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: AgentToolHandler
    allowed_agents: Optional[Tuple[str, ...]] = None
    retry_safe: bool = True
    timeout_s: Optional[float] = None
    # 处理函数内部已有熔断、重试与兜底（如经 MCPToolManager 的知识检索），执行层只保留总时限。
    self_governed: bool = False
    # 返回内容来自外部、可能被他人写入（知识库文档、外部 MCP、邮件网页），需要做提示注入检测。
    untrusted_output: bool = False


def make_tool(name: str, description: str, properties: Dict[str, Any], handler: AgentToolHandler, required: Optional[List[str]] = None, *, allowed_agents: Optional[Tuple[str, ...]] = None, retry_safe: bool = True, self_governed: bool = False,
              untrusted_output: bool = False) -> AgentToolSpec:
    schema = {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}
    return AgentToolSpec(name, description, schema, handler, allowed_agents, retry_safe, self_governed=self_governed, untrusted_output=untrusted_output)
