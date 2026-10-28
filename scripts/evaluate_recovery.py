"""Deterministic fault injection through Agent execution and monitor feedback.

Controlled local model failures, not production reliability or a throughput test.
"""

import asyncio
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from agents.agent_orchestrator import AgentOrchestrator, AgentType, Request
from monitor.performance_monitor import PerformanceMonitor
from tooling.tool_manager import MCPToolManager
from evaluation.reporting import write_report


async def main():
    async def fail(messages, info):
        raise RuntimeError("injected model failure")

    async def good(messages, info):
        await asyncio.sleep(0.01)
        return ModelResponse(parts=[TextPart("工具恢复，继续安全诊断。")])

    orchestrator = AgentOrchestrator("test")
    network = orchestrator._pool[AgentType.NETWORK_SOFTWARE][0]
    network._llm = FunctionModel(fail)
    orchestrator._pool[AgentType.TRIAGE][0]._llm = FunctionModel(good)
    monitor = PerformanceMonitor(orchestrator, MCPToolManager("test"), webhook_url="")
    for i in range(10):
        await orchestrator._execute(
            Request("VPN故障", request_id=f"fault-{i}"), AgentType.NETWORK_SOFTWARE
        )
    monitor.tick()
    cooling = network.stats.state()
    fallback = await orchestrator._execute(Request("VPN故障"), AgentType.NETWORK_SOFTWARE)
    network._llm = FunctionModel(good)
    network.stats.retry_at = (
        time.monotonic() - 1
    )  # advance cooldown only; exercise real acquisition/recovery
    probe, competing = await asyncio.gather(
        orchestrator._execute(Request("VPN故障", request_id="probe"), AgentType.NETWORK_SOFTWARE),
        orchestrator._execute(
            Request("VPN故障", request_id="competing"), AgentType.NETWORK_SOFTWARE
        ),
    )
    checks = {
        "cooled_after_failures": cooling == "cooling",
        "triage_fallback": fallback.agent_type == AgentType.TRIAGE,
        "single_probe": probe.agent_type == AgentType.NETWORK_SOFTWARE
        and competing.agent_type == AgentType.TRIAGE,
        "recovered": network.stats.state() == "healthy",
    }
    write_report(
        "recovery_v2.json",
        {
            "suite": "recovery-v2",
            "status": "passed" if all(checks.values()) else "failed",
            "injected_failures": 10,
            "checks": checks,
            "limitations": "Local FunctionModel faults and advanced cooldown clock; not measured wall-clock MTTR or production availability.",
        },
    )


if __name__ == "__main__":
    asyncio.run(main())
