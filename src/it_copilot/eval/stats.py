"""Percentile bootstrap of means or relative changes in means."""

import math
import statistics

import numpy as np

from .ledger import Arm, Delta, MetricSummary


def percentile(values, p):
    if not len(values) or not 0 <= p <= 100:
        raise ValueError("expected observations and a percentile in [0,100]")
    return sorted(values)[max(0, math.ceil(len(values) * p / 100) - 1)]


def bootstrap(x, y=None, *, paired=True, seed=42, relative=True):
    rng = np.random.default_rng(seed)
    x = np.asarray(x, dtype=float)
    indices = rng.integers(len(x), size=(1000, len(x)))
    estimates = x[indices].mean(axis=1)
    if y is not None:
        if paired and len(y) != len(x):
            raise ValueError("paired observations must align")
        if not paired:
            indices = rng.integers(len(y), size=(1000, len(y)))
        treatment = np.asarray(y)[indices].mean(axis=1)
        if relative:
            if np.any(estimates == 0):
                return None, None
            estimates = (treatment / estimates - 1) * 100
        else:
            estimates = treatment - estimates
    return tuple(float(percentile(estimates, p)) for p in (2.5, 97.5))


def summary(values):
    values = list(map(float, values))
    return MetricSummary(
        statistics.median(values),
        statistics.mean(values),
        *(percentile(values, p) for p in (50, 95, 99)),
        percentile(values, 75) - percentile(values, 25),
        *bootstrap(values),
        values,
        len(values) < 30,
    )


def arm(name, observations):
    return Arm(name, {k: summary(v) for k, v in observations.items()})


def compare(exp, base, prod, metric, trade_off, *, paired=True):
    b, t = (a.metrics[metric] for a in (base, prod))
    ratio = t.mean / b.mean if b.mean else None
    exp.deltas.append(
        Delta(
            base.name,
            prod.name,
            metric,
            (ratio - 1) * 100 if ratio is not None else None,
            ratio,
            *bootstrap(b.samples, t.samples, paired=paired),
            trade_off,
            t.mean - b.mean,
            *bootstrap(b.samples, t.samples, paired=paired, relative=False),
            b.mean / t.mean if metric.endswith("_ms") and t.mean else None,
        )
    )


def recall_at_k(results, relevant, k):
    return len(set(results[:k]) & set(relevant)) / len(set(relevant)) if relevant else 0.0


def mrr_at_k(results, relevant, k):
    return next((1 / i for i, r in enumerate(results[:k], 1) if r in relevant), 0.0)


def ndcg_at_k(results, relevant, k):
    hits = set()
    gain = 0.0
    for i, r in enumerate(results[:k], 2):
        if r in relevant and r not in hits:
            gain += 1 / math.log2(i)
            hits.add(r)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(k, len(set(relevant)))))
    return gain / ideal if ideal else 0.0


def jains_fairness_index(values):
    squares = sum(v * v for v in values)
    return sum(values) ** 2 / (len(values) * squares) if squares else 0.0


def measured_rounds(repeats):
    """One discarded warmup followed by the requested number of measured rounds."""
    if repeats < 1:
        raise ValueError("at least one measured round is required")
    yield False
    yield from (True for _ in range(repeats))
