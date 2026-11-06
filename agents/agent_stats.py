"""Agent 运行统计：近期成功率与延迟窗口、降级冷却与单探针恢复、路由评分（由运行监控回写惩罚）。"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Optional

from core.env import env_float


@dataclass
class AgentStats:
    """累计统计 + 5 分钟滑动窗口，用于性能路由、监控降权和单探针恢复。

    降级状态机：healthy（retry_at=0）→ 监控降权超阈值进入 cooling（retry_at 未到，拒绝请求）
    → 冷却结束为 half-open，只放行一个探针请求 → 探针成功回到 healthy，失败重新 cooling。
    """
    total: int = 0
    success: int = 0
    total_ms: float = 0.
    monitor_penalty: float = 0.
    samples: Any = field(default_factory=lambda: deque(maxlen=50))
    retry_at: float = 0.
    probe_running: bool = False
    probe_owner: Optional[str] = None

    def recent(self):
        cutoff = time.monotonic() - 300
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        n = len(self.samples)
        if not n:
            return 0, 1., 0.
        return n, sum(x[1] for x in self.samples) / n, sum(x[2] for x in self.samples) / n

    def available(self) -> bool:
        return not self.probe_running and (not self.retry_at or time.monotonic() >= self.retry_at)

    @property
    def degraded(self) -> bool:
        return bool(self.retry_at)

    def state(self) -> str:
        if not self.retry_at:
            return "healthy"
        if self.probe_running:
            return "probing"
        return "cooling" if time.monotonic() < self.retry_at else "half_open"

    def try_acquire(self, request_id: Optional[str]) -> bool:
        """健康时直接放行；half-open 时把本请求登记为唯一探针；其余情况拒绝。"""
        if not self.retry_at:
            return True
        if self.probe_running or time.monotonic() < self.retry_at:
            return False
        self.probe_running, self.probe_owner = True, request_id
        return True

    def set_penalty(self, penalty: float) -> None:
        self.monitor_penalty = max(0., min(.9, float(penalty)))
        if self.monitor_penalty >= env_float("IT_COPILOT_MONITOR_FALLBACK_PENALTY", .5) and not self.retry_at:
            self.retry_at = time.monotonic() + max(1, env_float("IT_COPILOT_MONITOR_COOLDOWN_SECONDS", 60))

    def record(self, success: bool, ms: float, request_id: Optional[str] = None) -> None:
        if self.probe_running and request_id == self.probe_owner:
            self.probe_running = False
            self.probe_owner = None
            if success:
                self.samples.clear()
                self.retry_at = 0.
                self.monitor_penalty = 0.
            else:
                self.retry_at = time.monotonic() + max(1, env_float("IT_COPILOT_MONITOR_COOLDOWN_SECONDS", 60))
        self.samples.append((time.monotonic(), int(success), ms))

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.total if self.total else 0.

    def routing_score(self) -> float:
        return (self.success_rate * .7 + (1 / (1 + self.avg_ms / 1000)) * .3) * max(0., 1 - self.monitor_penalty)
