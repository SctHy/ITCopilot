"""Serializable P0 experiment ledger. Relative changes are treatment / baseline."""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
import hashlib
import os
import platform
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "evaluation/datasets/rag_benchmark_v1.json"


@dataclass
class EnvMetadata:
    git_sha: str
    git_dirty: bool
    python_version: str
    platform: str
    cpu_count: int | None
    timestamp: str
    profile: str
    repeats: int
    datasets: list[dict]

    @classmethod
    def capture(cls, profile, repeats, paths):
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

        return cls(
            git("rev-parse", "HEAD"),
            bool(git("status", "--porcelain")),
            platform.python_version(),
            platform.platform(),
            os.cpu_count(),
            datetime.now(UTC).isoformat(),
            profile,
            repeats,
            [
                {
                    "path": str(p.relative_to(ROOT)),
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                }
                for p in paths
            ],
        )


@dataclass
class MetricSummary:
    median: float
    mean: float
    p50: float
    p95: float
    p99: float
    iqr: float
    ci95_low: float
    ci95_high: float
    samples: list[float]
    low_sample_warning: bool


@dataclass
class Delta:
    baseline: str
    treatment: str
    metric: str
    change_pct: float | None
    multiple: float | None
    ci95_low: float | None
    ci95_high: float | None
    trade_off: str
    difference: float
    difference_ci95_low: float
    difference_ci95_high: float
    speedup: float | None


@dataclass
class InvariantCheck:
    name: str
    observed: float
    expected: float

    @property
    def passed(self):
        return self.observed == self.expected


@dataclass
class Arm:
    name: str
    metrics: dict[str, MetricSummary]


@dataclass
class Experiment:
    name: str
    settings: dict = field(default_factory=dict)
    arms: list[Arm] = field(default_factory=list)
    deltas: list[Delta] = field(default_factory=list)
    checks: list[InvariantCheck] = field(default_factory=list)

    @property
    def passed(self):
        return bool(self.checks) and all(c.passed for c in self.checks)


@dataclass
class Report:
    env: EnvMetadata
    experiments: list[Experiment]

    def serialize(self):
        obj = asdict(self)
        for row, exp in zip(obj["experiments"], self.experiments):
            row["passed"] = exp.passed
            for check, gate in zip(row["checks"], exp.checks):
                check["passed"] = gate.passed
        obj["passed"] = bool(self.experiments) and all(e.passed for e in self.experiments)
        return obj
