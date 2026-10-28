"""MCP round trip over real Streamable HTTP: our client ↔ our server on a local port; no external network."""
import asyncio
import pathlib
import socket
import threading
import time

import pytest
import uvicorn

from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request
from tooling.mcp_client import MCPServerConfig, StandardMCPClient, load_servers
from tooling.mcp_server import create_http_app, create_server
from tooling.tool_manager import MCPToolManager

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeKnowledgeBase:
    async def search_async(self, query, top_k=5, **kwargs):
        return [{"title": "VPN 排障 SOP", "content": f"{query}：检查账号状态与系统时间", "citation": "kb://vpn#1", "score": 0.9}][:top_k]

    async def doc_count_async(self):
        return 1


@pytest.fixture(scope="module")
def mcp_url():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_http_app(create_server(FakeKnowledgeBase())), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "MCP test server did not start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/"
    server.should_exit = True
    thread.join(timeout=5)


def config(url, **overrides):
    values = {"name": "local", "transport": "streamable_http", "url": url, "allowed_tools": ["search_local_knowledge"], "agents": ["network_software"], "timeout_s": 10}
    return MCPServerConfig(**{**values, **overrides})


def test_shipped_config_is_valid():
    servers = load_servers(ROOT / "config/mcp_servers.json")
    assert [s.name for s in servers] == ["mslearn"] and all(s.read_only for s in servers)


def test_client_discovers_registers_and_calls_remote_tool(mcp_url):
    async def run():
        manager = MCPToolManager("sk-local")
        orchestrator = AgentOrchestrator("sk-local")
        client = StandardMCPClient(manager, [config(mcp_url)], on_change=lambda: orchestrator.set_external_tools(client.agent_tools()))
        await client.start()
        try:
            server = client.summary()["servers"][0]
            assert server["state"] == "connected" and server["protocol_version"]
            alias = "mcp_local_search_local_knowledge"
            assert [t["name"] for t in server["tools"]] == [alias]

            # 只进入 allowed_agents 中的 Agent
            assert alias in orchestrator._pool[AgentType.NETWORK_SOFTWARE][0].get_tools()
            assert alias not in orchestrator._pool[AgentType.ACCESS][0].get_tools()

            spec = client.agent_tools()[alias]
            assert spec.retry_safe and spec.input_schema["properties"]["query"]["type"] == "string"
            result = await spec.handler(Request("VPN 连不上"), {"query": "VPN-691", "top_k": 1})
            assert result["success"] and result["source"] == "external_mcp"
            assert result["data"]["structured"]["results"][0]["citation"] == "kb://vpn#1"

            cached = await spec.handler(Request("VPN 连不上"), {"query": "VPN-691", "top_k": 1})
            assert cached["cached"] is True                                   # 走治理层 TTL 缓存
            invalid = await spec.handler(Request("x"), {"query": ""})
            assert not invalid["success"] and "Schema" in invalid["error"]    # 远端 schema 在本地校验
        finally:
            await client.close()
        assert client.summary()["servers"][0]["state"] == "closed"
        assert "mcp_local_search_local_knowledge" not in orchestrator._pool[AgentType.NETWORK_SOFTWARE][0].get_tools()
        assert "mcp_local_search_local_knowledge" not in manager._tools

    asyncio.run(run())


def test_tools_outside_allowlist_are_ignored(mcp_url):
    async def run():
        manager = MCPToolManager("sk-local")
        client = StandardMCPClient(manager, [config(mcp_url, allowed_tools=["something_else"])])
        await client.start()
        try:
            assert client.summary()["servers"][0]["state"] == "connected"
            assert client.agent_tools() == {}
        finally:
            await client.close()

    asyncio.run(run())


def test_unreachable_server_degrades_without_blocking():
    async def run():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        client = StandardMCPClient(MCPToolManager("sk-local"), [config(f"http://127.0.0.1:{port}/", timeout_s=2)])
        started = time.monotonic()
        await client.start(wait=False)
        assert time.monotonic() - started < 0.5
        await asyncio.sleep(1)
        server = client.summary()["servers"][0]
        assert server["state"] == "disconnected" and server["error"]
        await client.close()

    asyncio.run(run())
