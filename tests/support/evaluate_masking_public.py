from __future__ import annotations


import hashlib


from pathlib import Path


import statistics


from tests.support.masking_benchmark import rates

PREFIX = "Document:\n"  # A fixed wrapper prevents JSON pretty-printing from changing offsets.

SUM_KEYS = (
    "gold",
    "predicted",
    "tp",
    "fp",
    "fn",
    "untyped_tp",
    "fully_covered",
    "gold_chars",
    "covered_gold_chars",
    "masked_non_gold_chars",
    "leaking_document",
    "negative_document",
    "negative_changed",
)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def latency(values):
    ordered = sorted(values)
    return {
        "p50_ms": statistics.median(ordered),
        "p95_ms": ordered[int(0.95 * (len(ordered) - 1))],
        "p99_ms": ordered[int(0.99 * (len(ordered) - 1))],
    }


def aggregate(records):
    counts = {key: sum(r[key] for r in records) for key in SUM_KEYS}
    result = rates(counts)
    positive = sum(r["gold"] > 0 for r in records)
    result.update(
        documents=len(records),
        sensitive_documents=positive,
        document_leak_rate=counts["leaking_document"] / positive if positive else None,
        negative_change_rate=counts["negative_changed"] / counts["negative_document"]
        if counts["negative_document"]
        else None,
        blocked=sum(r["blocked"] for r in records),
        restoration_failed=sum(not r["restoration_ok"] for r in records),
        latency=latency([r["latency_ms"] for r in records]),
    )
    # Document bootstrap, conditional on this synthetic corpus, not a production CI.
    import numpy as np

    rng = np.random.default_rng(20261114)
    a = np.array([[r["tp"], r["gold"], r["predicted"], r["fully_covered"]] for r in records])
    sums = np.array([a[rng.integers(0, len(a), len(a))].sum(axis=0) for _ in range(1000)])
    for name, numerator, denominator in [
        ("f1", 2 * sums[:, 0], sums[:, 1] + sums[:, 2]),
        ("full_coverage", sums[:, 3], sums[:, 1]),
    ]:
        vals = np.divide(numerator, denominator, out=np.zeros(1000), where=denominator != 0)
        result[name + "_bootstrap_95ci"] = np.quantile(vals, [0.025, 0.975]).tolist()
    return result
