"""Agent 运行统计：累计计数与 5 分钟滑动窗口，供运行监控判断近期成功率与延迟。"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

WINDOW_S = 300


@dataclass
class AgentStats:
    total: int = 0
    success: int = 0
    total_ms: float = 0.
    samples: Any = field(default_factory=lambda: deque(maxlen=50))

    def recent(self):
        """(次数, 成功率, 平均耗时 ms)，只统计近 5 分钟。"""
        cutoff = time.monotonic() - WINDOW_S
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        n = len(self.samples)
        if not n:
            return 0, 1., 0.
        return n, sum(x[1] for x in self.samples) / n, sum(x[2] for x in self.samples) / n

    def record(self, success: bool, ms: float) -> None:
        self.samples.append((time.monotonic(), int(success), ms))

    @property
    def success_rate(self) -> float:
        return self.success / self.total if self.total else 1.

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.total if self.total else 0.
