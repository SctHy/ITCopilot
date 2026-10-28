"""将本地 IT 知识库以标准 MCP Tools / Resources 暴露给其他客户端（Claude Desktop、Cursor、其他 Agent）。

API 在 /mcp 挂载 Streamable HTTP 端点；检索经过 MCPToolManager 的 Schema 校验、缓存、超时与熔断，
与 Agent 内部检索共用同一条治理链路。只读工具，带 readOnlyHint 注解。
"""
import json
import os
from typing import Annotated, Any, Dict, List

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

DEFAULT_ALLOWED_HOSTS = "localhost,localhost:*,127.0.0.1,127.0.0.1:*,api,api:*"
DEFAULT_ALLOWED_ORIGINS = "http://localhost,http://localhost:*,http://127.0.0.1,http://127.0.0.1:*"


class KnowledgeSearchResult(BaseModel):
    results: List[Dict[str, Any]]
    source: str = "it-copilot_knowledge"


def _csv(name: str, default: str) -> List[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def create_server(knowledge_base, tool_manager=None) -> MCPServer:
    server = MCPServer("IT-Copilot Knowledge", instructions="企业 IT 服务台知识库：VPN、账号、权限、设备与工单 SOP。")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True))
    async def search_local_knowledge(query: Annotated[str, Field(min_length=1, max_length=2000)], top_k: Annotated[int, Field(ge=1, le=8, strict=True)] = 5) -> KnowledgeSearchResult:
        """检索 IT-Copilot 本地 IT 知识库，返回文档标题、片段、citation 和检索分数。"""
        query = query.strip()
        if not query:
            raise ToolError("query 不能为空")
        if tool_manager is not None:
            result = await tool_manager.call("knowledge_search", {"query": query, "top_k": top_k})
            if not result.success:
                raise ToolError("知识库检索失败，请稍后重试")
            return KnowledgeSearchResult(results=result.data)
        return KnowledgeSearchResult(results=await knowledge_base.search_async(query, top_k))

    @server.resource("it-copilot://knowledge/stats")
    async def knowledge_stats() -> str:
        """读取知识库片段数量。"""
        return json.dumps({"total_chunks": await knowledge_base.doc_count_async()})

    return server


def create_http_app(server: MCPServer):
    """Streamable HTTP ASGI 应用（挂载在 /mcp 下，故内部路径为 /）。开启 DNS rebinding 防护。"""
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_csv("MCP_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS),
        allowed_origins=_csv("MCP_ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS),
    )
    return server.streamable_http_app(streamable_http_path="/", stateless_http=True, json_response=True, transport_security=security)
