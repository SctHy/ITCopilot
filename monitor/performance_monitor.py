"""Agent 在线表现监控：近期窗口规则 + SLO 燃烧率告警 + 告警状态机 + 路由反馈闭环。

数据来源（无需额外埋点）：
  - Orchestrator.get_stats() 的 5 分钟滑动窗口（recent_*）
  - MCPToolManager.get_stats() 的累计计数，按快照差分得到近期成功率
  - API 每次 /chat 结束时调用 record_chat()，驱动 SLO 时间窗
  - StandardMCPClient.summary() 的外部 MCP 连接状态（可选）

闭环：近期成功率/延迟恶化 → update_routing_penalties() 降权 → 超阈值的专职 Agent 进入冷却，
请求改由 Triage 兜底 → 冷却结束放行单个探针 → 成功即恢复。

告警按“状态迁移”通知（触发 / 升级 / 恢复各一次），连续 RESOLVE_TICKS 个周期正常才恢复，避免抖动刷屏。
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import httpx

from monitor import metrics

logger = logging.getLogger(__name__)

SEVERITY_ORDER = {"warning": 1, "critical": 2}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── SLO 与错误预算 ────────────────────────────────────────────────────────────

class SLOTracker:
    """按固定宽度时间桶累计好/坏事件，计算任意窗口内的错误率与燃烧率。

    燃烧率 = 窗口错误率 / 错误预算（1 - target）；燃烧率 1 表示恰好在 SLO 周期末用完预算。
    """

    def __init__(self, name: str, target: float, bucket_s: float = 10, horizon_s: float = 6 * 3600):
        if not 0 < target < 1:
            raise ValueError("SLO target 必须在 (0, 1) 之间")
        self.name, self.target, self.bucket_s = name, target, bucket_s
        self._buckets: Deque[List[float]] = deque(maxlen=math.ceil(horizon_s / bucket_s) + 1)  # [bucket_start, total, bad]

    def record(self, good: bool, now: Optional[float] = None) -> None:
        start = (now if now is not None else time.monotonic()) // self.bucket_s * self.bucket_s
        if not self._buckets or start > self._buckets[-1][0]:
            self._buckets.append([start, 0, 0])
        # 时钟回退或乱序到达的事件并入最新桶，保持桶按时间单调递增。
        self._buckets[-1][1] += 1
        self._buckets[-1][2] += 0 if good else 1

    def window(self, seconds: float, now: Optional[float] = None) -> Tuple[int, int]:
        cutoff = (now if now is not None else time.monotonic()) - seconds
        total = bad = 0
        for start, count, failed in reversed(self._buckets):
            if start + self.bucket_s <= cutoff:
                break
            total, bad = total + count, bad + failed
        return int(total), int(bad)

    def burn_rate(self, seconds: float, now: Optional[float] = None) -> float:
        total, bad = self.window(seconds, now)
        return (bad / total) / (1 - self.target) if total else 0.


# Google SRE Workbook 推荐的多窗口多燃烧率组合：长窗口确认趋势，短窗口保证恢复后尽快消警。
BURN_RATE_RULES = (
    ("critical", 3600, 300, 14.4),   # 1h 内消耗 2% 月度预算
    ("warning", 6 * 3600, 1800, 6.0),
)
WINDOW_LABELS = {300: "5m", 1800: "30m", 3600: "1h", 6 * 3600: "6h"}


# ── 告警状态机 ────────────────────────────────────────────────────────────────

@dataclass
class Condition:
    rule: str
    severity: str
    message: str
    value: float
    threshold: float
    action: str = ""


@dataclass
class Alert:
    key: str
    rule: str
    severity: str
    message: str
    value: float
    threshold: float
    action: str = ""
    state: str = "firing"
    started_at: str = field(default_factory=_now_iso)
    resolved_at: Optional[str] = None
    clear_ticks: int = 0


class AlertManager:
    RESOLVE_TICKS = 2

    def __init__(self, notify: Optional[Callable[[str, Alert], None]] = None, history: int = 200):
        self.firing: Dict[str, Alert] = {}
        self.history: Deque[Alert] = deque(maxlen=history)
        self._notify = notify or (lambda event, alert: None)

    def evaluate(self, active: Dict[str, Condition]) -> None:
        for key, cond in active.items():
            alert = self.firing.get(key)
            if alert is None:
                alert = Alert(key, cond.rule, cond.severity, cond.message, cond.value, cond.threshold, cond.action)
                self.firing[key] = alert
                self._emit("firing", alert)
                continue
            escalated = SEVERITY_ORDER[cond.severity] > SEVERITY_ORDER[alert.severity]
            alert.severity, alert.message, alert.value, alert.threshold, alert.action = cond.severity, cond.message, cond.value, cond.threshold, cond.action
            alert.clear_ticks = 0
            self._set_gauge(alert)
            if escalated:
                self._emit("escalated", alert)
        for key, alert in list(self.firing.items()):
            if key in active:
                continue
            alert.clear_ticks += 1
            if alert.clear_ticks >= self.RESOLVE_TICKS:
                alert.state, alert.resolved_at = "resolved", _now_iso()
                del self.firing[key]
                self.history.append(alert)
                self._set_gauge(alert)
                self._emit("resolved", alert)

    @staticmethod
    def _set_gauge(alert: Alert) -> None:
        for severity in SEVERITY_ORDER:
            metrics.ALERTS_FIRING.labels(alert=alert.key, severity=severity).set(int(alert.state == "firing" and severity == alert.severity))

    def _emit(self, event: str, alert: Alert) -> None:
        if event == "firing":
            self._set_gauge(alert)
        log = logger.warning if event != "resolved" else logger.info
        log("[alert %s] %s %s", event, alert.severity.upper(), alert.message)
        self._notify(event, alert)


# ── 监控器 ────────────────────────────────────────────────────────────────────

@dataclass
class Thresholds:
    min_samples: int = 5
    agent_success_warning: float = .90
    agent_success_critical: float = .70
    agent_latency_ms: float = 8000
    tool_success_warning: float = .95
    tool_window_s: float = 300
    routing_success: float = .90
    routing_latency_ms: float = 8000

    @classmethod
    def from_env(cls) -> "Thresholds":
        return cls(
            agent_latency_ms=_env_float("IT_COPILOT_ALERT_AGENT_LATENCY_MS", 8000),
            routing_latency_ms=_env_float("IT_COPILOT_ROUTING_LATENCY_MS", 8000),
        )


class PerformanceMonitor:
    def __init__(self, orchestrator, tool_manager, mcp_client=None, *, interval_s: Optional[float] = None, webhook_url: Optional[str] = None,
                 availability_target: Optional[float] = None, latency_target: Optional[float] = None, latency_threshold_s: Optional[float] = None,
                 thresholds: Optional[Thresholds] = None):
        self._orchestrator = orchestrator
        self._tool_manager = tool_manager
        self._mcp_client = mcp_client
        self._interval = interval_s if interval_s is not None else _env_float("IT_COPILOT_MONITOR_INTERVAL_S", 10)
        self._webhook = webhook_url if webhook_url is not None else os.getenv("IT_COPILOT_ALERT_WEBHOOK") or None
        self._latency_threshold_s = latency_threshold_s if latency_threshold_s is not None else _env_float("IT_COPILOT_SLO_LATENCY_S", 10)
        self.thresholds = thresholds or Thresholds.from_env()
        self.slos = {
            "chat_availability": SLOTracker("chat_availability", availability_target or _env_float("IT_COPILOT_SLO_AVAILABILITY", .95)),
            "chat_latency": SLOTracker("chat_latency", latency_target or _env_float("IT_COPILOT_SLO_LATENCY_TARGET", .95)),
        }
        self.alerts = AlertManager(self._notify)
        self._tool_snapshots: Deque[Tuple[float, Dict[str, Tuple[int, int]]]] = deque(maxlen=512)
        self._tasks: set = set()
        self._loop_task: Optional[asyncio.Task] = None
        self.last_tick: Optional[str] = None

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._loop())
            logger.info("Monitor 已启动，采集间隔 %ss", self._interval)

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("Monitor 采集异常")
            await asyncio.sleep(self._interval)

    # ── 事件输入 ──────────────────────────────────────────────────────────────

    def record_chat(self, status: str, latency_s: float, now: Optional[float] = None) -> None:
        self.slos["chat_availability"].record(status in {"success", "partial"}, now)
        self.slos["chat_latency"].record(latency_s <= self._latency_threshold_s, now)

    # ── 单次评估 ──────────────────────────────────────────────────────────────

    def tick(self, now: Optional[float] = None) -> Dict[str, Condition]:
        now = now if now is not None else time.monotonic()
        active: Dict[str, Condition] = {}
        active.update(self._slo_conditions(now))
        active.update(self._agent_conditions())
        active.update(self._tool_conditions(now))
        active.update(self._mcp_conditions())
        self.alerts.evaluate(active)
        self.last_tick = _now_iso()
        return active

    def _slo_conditions(self, now: float) -> Dict[str, Condition]:
        out = {}
        for name, slo in self.slos.items():
            for seconds in WINDOW_LABELS:
                metrics.SLO_BURN_RATE.labels(slo=name, window=WINDOW_LABELS[seconds]).set(slo.burn_rate(seconds, now))
            for severity, long_s, short_s, factor in BURN_RATE_RULES:
                long_rate, short_rate = slo.burn_rate(long_s, now), slo.burn_rate(short_s, now)
                if slo.window(short_s, now)[0] >= self.thresholds.min_samples and long_rate > factor and short_rate > factor:
                    out[f"slo_burn:{name}"] = Condition(
                        "slo_burn_rate", severity,
                        f"{name} 错误预算燃烧率 {WINDOW_LABELS[long_s]}={long_rate:.1f}x / {WINDOW_LABELS[short_s]}={short_rate:.1f}x（阈值 {factor}x，目标 {slo.target:.2%}）",
                        round(long_rate, 2), factor, "按 /trace 回放最近失败请求定位主要错误码；网关类错误优先检查 8082 网关与上游配额。",
                    )
                    break
        return out

    def _agent_conditions(self) -> Dict[str, Condition]:
        t = self.thresholds
        out, penalties = {}, {}
        for key, s in self._orchestrator.get_stats().items():
            n, success, latency = s["recent_total"], s["recent_success_rate"], s["recent_avg_ms"]
            penalties[key] = self._routing_penalty(success, latency, t.routing_success, t.routing_latency_ms) if n >= t.min_samples else 0.
            if n >= t.min_samples and success < t.agent_success_warning:
                severity = "critical" if success < t.agent_success_critical else "warning"
                out[f"agent_success:{key}"] = Condition(
                    "agent_success_rate", severity, f"Agent {key} 近 5 分钟成功率 {success:.0%}（{n} 次）", round(success, 3), t.agent_success_warning,
                    "查看 it_copilot_agent_failures_total 的 reason 分布：gateway 查网关，round_limit 查提示词与工具设计，bad_output 查模型输出格式。",
                )
            if n >= t.min_samples and latency > t.agent_latency_ms:
                out[f"agent_latency:{key}"] = Condition(
                    "agent_latency", "warning", f"Agent {key} 近 5 分钟平均耗时 {latency:.0f}ms", round(latency), t.agent_latency_ms,
                    "检查上游模型延迟与工具耗时分布；必要时降低 max_tokens 或为该 Agent 换更快的模型。",
                )
        self._orchestrator.update_routing_penalties(penalties)
        for key, s in self._orchestrator.get_stats().items():
            degraded = s["state"] != "healthy"
            metrics.AGENT_DEGRADED.labels(agent=key).set(int(degraded))
            if degraded:
                out[f"agent_degraded:{key}"] = Condition(
                    "agent_degraded", "warning", f"Agent {key} 已降级（{s['state']}），请求由 Triage 兜底", s["monitor_penalty"], 0,
                    "冷却结束后会自动放行单个探针请求；持续降级说明根因未消除。",
                )
        return out

    def _tool_conditions(self, now: float) -> Dict[str, Condition]:
        t = self.thresholds
        stats = self._tool_manager.get_stats()
        current = {name: (s["total"], s["success"]) for name, s in self._tool_manager.monitor_snapshot().items()}
        self._tool_snapshots.append((now, current))
        while len(self._tool_snapshots) > 1 and self._tool_snapshots[1][0] <= now - t.tool_window_s:
            self._tool_snapshots.popleft()
        baseline = self._tool_snapshots[0][1]
        out = {}
        for name, s in stats.items():
            if s["circuit_state"] != "closed":
                out[f"tool_circuit:{name}"] = Condition(
                    "tool_circuit", "critical" if s["circuit_state"] == "open" else "warning", f"工具 {name} 熔断状态 {s['circuit_state']}（连续失败 {s['consecutive_fails']} 次）",
                    s["consecutive_fails"], 0, "检查工具依赖服务；熔断期间返回 fallback 结果，恢复后半开探测自动闭合。",
                )
            base_total, base_ok = baseline.get(name, (0, 0))
            total, ok = current[name][0] - base_total, current[name][1] - base_ok
            if total >= t.min_samples and ok / total < t.tool_success_warning:
                out[f"tool_success:{name}"] = Condition(
                    "tool_success_rate", "warning", f"工具 {name} 近 {t.tool_window_s:.0f}s 成功率 {ok / total:.0%}（{total} 次）", round(ok / total, 3), t.tool_success_warning,
                    "查看 it_copilot_tool_calls_total 的 outcome 分布区分超时、参数错误与依赖故障。",
                )
        return out

    def _mcp_conditions(self) -> Dict[str, Condition]:
        if self._mcp_client is None:
            return {}
        out = {}
        for server in self._mcp_client.summary()["servers"]:
            if server["state"] != "connected":
                out[f"mcp_down:{server['name']}"] = Condition(
                    "mcp_server_down", "warning", f"外部 MCP 服务 {server['name']} 未连接（{server['error'] or server['state']}）", 0, 1,
                    "外部工具已自动下线，Agent 仅使用本地工具；客户端按指数退避自动重连。",
                )
        return out

    @staticmethod
    def _routing_penalty(success_rate: float, avg_ms: float, success_floor: float, latency_ms: float) -> float:
        """把近期表现转成 0–0.9 的路由降权；≥ IT_COPILOT_MONITOR_FALLBACK_PENALTY（默认 0.5）触发降级冷却。"""
        penalty = 0.
        if success_rate < success_floor:
            penalty += min(.6, (success_floor - success_rate) * 2)
        if avg_ms > latency_ms:
            penalty += min(.3, (avg_ms - latency_ms) / 20000)
        return min(penalty, .9)

    # ── 通知 ──────────────────────────────────────────────────────────────────

    def _notify(self, event: str, alert: Alert) -> None:
        if not self._webhook:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._send_webhook(event, alert))
        except RuntimeError:
            return  # 无事件循环（同步测试或脚本调用）时只记日志
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send_webhook(self, event: str, alert: Alert) -> None:
        payload = {"event": event, **{k: v for k, v in asdict(alert).items() if k != "clear_ticks"}}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(self._webhook, json=payload)
        except Exception as ex:
            logger.warning("告警 Webhook 发送失败: %s", type(ex).__name__)

    # ── 查询 ──────────────────────────────────────────────────────────────────

    def summary(self) -> Dict[str, Any]:
        now = time.monotonic()
        firing = sorted(self.alerts.firing.values(), key=lambda a: (-SEVERITY_ORDER[a.severity], a.started_at))
        slos = []
        for name, slo in self.slos.items():
            total, bad = slo.window(3600, now)
            slos.append({
                "name": name, "target": slo.target, "requests_1h": total, "bad_1h": bad,
                "burn_rate": {WINDOW_LABELS[s]: round(slo.burn_rate(s, now), 2) for s in WINDOW_LABELS},
            })
        return {
            "last_tick": self.last_tick,
            "status": "critical" if any(a.severity == "critical" for a in firing) else "degraded" if firing else "ok",
            "slos": slos,
            "alerts": [self._alert_view(a) for a in firing],
            "recent_resolved": [self._alert_view(a) for a in list(self.alerts.history)[-10:]],
            "suggestions": [{"alert": a.key, "severity": a.severity, "action": a.action} for a in firing if a.action],
        }

    @staticmethod
    def _alert_view(alert: Alert) -> Dict[str, Any]:
        return {k: v for k, v in asdict(alert).items() if k != "clear_ticks"}
