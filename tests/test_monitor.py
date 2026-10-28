"""PerformanceMonitor: SLO burn rate, alert state machine, routing feedback loop; deterministic clock, offline."""
import asyncio

import pytest

from agents.agent_orchestrator import AgentOrchestrator, AgentType
from monitor.performance_monitor import AlertManager, Condition, PerformanceMonitor, SLOTracker, Thresholds
from tooling.tool_manager import MCPToolManager, Tool

T0 = 100_000.0


def make_monitor(**kwargs):
    events = []
    monitor = PerformanceMonitor(AgentOrchestrator("sk-local"), MCPToolManager("sk-local"), interval_s=10, webhook_url="", availability_target=.95, **kwargs)
    monitor.alerts._notify = lambda event, alert: events.append((event, alert.key, alert.severity))
    return monitor, events


def test_slo_tracker_burn_rate_and_windows():
    slo = SLOTracker("x", target=.99, bucket_s=10)
    slo.record(False, now=T0 - 7200)             # 2 小时前的坏事件不应进入 1h 窗口
    for i in range(100):
        slo.record(i >= 2, now=T0)              # 2% 错误
    assert slo.window(3600, now=T0) == (100, 2)
    assert slo.burn_rate(3600, now=T0) == pytest.approx(2.0)
    assert slo.window(6 * 3600, now=T0) == (101, 3)
    assert SLOTracker("y", .9).burn_rate(300) == 0


def test_burn_rate_alert_pages_downgrades_and_resolves_as_windows_clear():
    monitor, events = make_monitor()
    for i in range(60):
        monitor.record_chat("failed" if i % 6 else "success", 1.0, now=T0)   # 83% 错误 ≈ 16.7x 预算（>14.4x）
    monitor.tick(now=T0 + 1)
    assert events == [("firing", "slo_burn:chat_availability", "critical")]
    assert monitor.summary()["status"] == "critical"

    for t in range(40):                                                      # 错误停止
        monitor.record_chat("success", 1.0, now=T0 + 320 + t)
    monitor.tick(now=T0 + 400)                                               # 5m 窗口已干净：critical 条件解除
    alert = monitor.alerts.firing["slo_burn:chat_availability"]
    assert alert.severity == "warning"                                       # 30m 窗口仍在燃烧：降级为 warning

    monitor.tick(now=T0 + 1900)                                              # 30m 窗口也干净
    monitor.tick(now=T0 + 1910)
    assert events[-1] == ("resolved", "slo_burn:chat_availability", "warning")
    assert monitor.alerts.firing == {}


def test_latency_slo_counts_slow_requests_as_bad():
    monitor, events = make_monitor(latency_threshold_s=2)
    for _ in range(30):
        monitor.record_chat("success", 9.0, now=T0)
    monitor.tick(now=T0 + 1)
    assert any(key == "slo_burn:chat_latency" for _, key, _ in events)


def test_alert_manager_notifies_on_transitions_only():
    events = []
    manager = AlertManager(lambda event, alert: events.append((event, alert.severity)))
    warn = {"k": Condition("r", "warning", "m", 1, 0)}
    for _ in range(3):
        manager.evaluate(warn)
    manager.evaluate({"k": Condition("r", "critical", "m", 2, 0)})
    manager.evaluate({})
    assert "k" in manager.firing                      # 一次正常不足以恢复（防抖）
    manager.evaluate(warn)                            # 再次异常：清零计数，不重复通知
    manager.evaluate({})
    manager.evaluate({})
    assert events == [("firing", "warning"), ("escalated", "critical"), ("resolved", "warning")]
    assert manager.history[-1].state == "resolved"


def test_failing_agent_alerts_degrades_and_routes_to_triage():
    monitor, events = make_monitor()
    agent = monitor._orchestrator._pool[AgentType.NETWORK_SOFTWARE][0]
    for i in range(10):
        agent.stats.record(False, 500, f"r{i}")
    monitor.tick()
    keys = {key for _, key, _ in events}
    assert {"agent_success:network_software_0", "agent_degraded:network_software_0"} <= keys
    assert agent.stats.state() == "cooling" and agent.stats.monitor_penalty >= .5
    summary = monitor.summary()
    assert summary["status"] == "critical" and any(s["alert"] == "agent_success:network_software_0" for s in summary["suggestions"])


def test_tool_rules_use_recent_window_not_lifetime_totals():
    monitor, events = make_monitor(thresholds=Thresholds(tool_window_s=300))
    manager = monitor._tool_manager

    def flaky(params, ctx):
        raise RuntimeError("downstream 503")

    manager.register(Tool("monitor_flaky", "flaky", flaky, {"type": "object"}))
    manager._tools["monitor_flaky"].breaker.threshold = 100   # 只看成功率规则
    tool = manager._tools["monitor_flaky"]
    tool.stats.total = tool.stats.success = 1000              # 历史表现极好
    monitor.tick(now=T0)

    async def burst():
        for _ in range(6):
            await manager.call("monitor_flaky", {})
    asyncio.run(burst())
    monitor.tick(now=T0 + 10)
    assert ("firing", "tool_success:monitor_flaky", "warning") in events  # 累计成功率仍 >99%，近期 0%


def test_open_circuit_is_critical():
    monitor, events = make_monitor()
    manager = monitor._tool_manager
    manager.register(Tool("monitor_down", "down", lambda p, c: (_ for _ in ()).throw(RuntimeError("x")), {"type": "object"}))
    manager._tools["monitor_down"].breaker.threshold = 1

    asyncio.run(manager.call("monitor_down", {}))
    monitor.tick(now=T0)
    assert ("firing", "tool_circuit:monitor_down", "critical") in events


def test_mcp_disconnect_raises_warning():
    class FakeMCP:
        def summary(self):
            return {"servers": [{"name": "mslearn", "state": "disconnected", "error": "ConnectError"}]}

    monitor, events = make_monitor()
    monitor._mcp_client = FakeMCP()
    monitor.tick()
    assert ("firing", "mcp_down:mslearn", "warning") in events


def test_webhook_receives_transition_payload():
    sent = []

    async def run():
        monitor = PerformanceMonitor(AgentOrchestrator("sk-local"), MCPToolManager("sk-local"), webhook_url="http://hooks.local/alert")

        async def fake_send(event, alert):
            sent.append((event, alert.key))

        monitor._send_webhook = fake_send
        monitor.alerts.evaluate({"k": Condition("r", "warning", "m", 1, 0)})
        monitor.alerts.evaluate({"k": Condition("r", "warning", "m", 1, 0)})
        await asyncio.sleep(0)
        await monitor.stop()

    asyncio.run(run())
    assert sent == [("firing", "k")]
