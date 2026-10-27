"""Small, explainable temperature scaling for intent confidence.

The fitter uses one temperature per intent group. It only calibrates the
confidence value; it never changes the predicted intent.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, Iterable, Mapping, Sequence


def _clip(value: float) -> float:
    return min(1.0 - 1e-6, max(1e-6, float(value)))


def _logit(value: float) -> float:
    p = _clip(value)
    return math.log(p / (1.0 - p))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def calibrate(confidence: float, temperature: float) -> float:
    """Apply temperature scaling to a probability-like confidence."""
    return round(_sigmoid(_logit(confidence) / max(0.05, float(temperature))), 4)


def _nll(rows, temperature: float) -> float:
    loss = 0.0
    for confidence, correct in rows:
        p = _clip(calibrate(confidence, temperature))
        loss -= math.log(p if correct else 1.0 - p)
    return loss / max(1, len(rows))


def fit_temperatures(rows_by_group: Mapping[str, Sequence[tuple[float, bool]]]) -> Dict[str, float]:
    """Fit one scalar T per group using a deterministic grid search."""
    result = {}
    for group, rows in rows_by_group.items():
        if not rows:
            continue
        candidates = [0.25 + i * 0.05 for i in range(0, 96)]
        result[group] = round(min(candidates, key=lambda t: _nll(rows, t)), 4)
    return result


def ece(rows: Iterable[tuple[float, bool]], bins: int = 10) -> float:
    rows = list(rows)
    if not rows:
        return 0.0
    total = len(rows); value = 0.0
    for index in range(bins):
        lo, hi = index / bins, (index + 1) / bins
        bucket = [(p, ok) for p, ok in rows if lo <= p < hi or (index == bins - 1 and p == hi)]
        if bucket:
            value += len(bucket) / total * abs(sum(p for p, _ in bucket) / len(bucket) - sum(ok for _, ok in bucket) / len(bucket))
    return round(value, 6)


def brier(rows: Iterable[tuple[float, bool]]) -> float:
    rows = list(rows)
    return round(sum((p - float(ok)) ** 2 for p, ok in rows) / max(1, len(rows)), 6)


def load_temperatures(path: str | Path | None) -> Dict[str, float]:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in (data.get("temperatures") or {}).items()}
    except (OSError, ValueError, TypeError):
        return {}
