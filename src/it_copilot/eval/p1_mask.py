"""Score the actual emitted placeholders in original Python character offsets."""

import json
import time
from collections import Counter

import numpy as np

from it_copilot.agent.masking import local_pii
from it_copilot.agent.masking.model_masking import MaskingSession, _TOKEN
from .p1 import experiment, NAMES
from .stats import percentile

LABELS = ("regex_only", "model_only", "hybrid_prod")
NORMALIZE = dict(PASSWORD="SECRET", API_KEY="SECRET", TOKEN="SECRET", PRIVATE_KEY="SECRET")


def document(row, label, recognizer):
    text = row["text"]
    session = MaskingSession()
    start = time.perf_counter()
    spans = (
        ()
        if label == LABELS[0]
        else tuple(
            s
            for s in recognizer.detect(
                text,
                deadline=time.monotonic() + 10,
                use_cache=False,
                supplement_names=label == LABELS[2],
            )
            if s.score >= 0.8
        )
    )
    if label == LABELS[1]:
        end = 0
        parts = []
        for s in sorted(spans, key=lambda s: (s.start, -s.end)):
            if s.start >= end:
                parts.extend([text[end : s.start], session.remember(text[s.start : s.end], s.kind)])
                end = s.end
        masked = "".join(parts) + text[end:]
    else:
        session.findings = {text: spans}
        session.learn(text)
        masked = session.mask(text)
    latency = (time.perf_counter() - start) * 1000
    pred = set()
    offset = previous = 0
    for token in _TOKEN.finditer(masked):
        raw = session.values[token.group()]
        kind = token[1].rsplit("_", 1)[0]
        offset += token.start() - previous
        pred.add((offset, offset + len(raw), NORMALIZE.get(kind, kind)))
        offset += len(raw)
        previous = token.end()
    gold = {(s["start"], s["end"], s["type"]) for s in row["entities"]}
    chars = {i for a, b, _ in pred for i in range(a, b)}
    restored = json.loads(
        session.restore(json.dumps({"argument": masked}, ensure_ascii=False), tool=True)
    )
    counts = Counter(
        tp=len(gold & pred),
        pred=len(pred),
        gold=len(gold),
        covered=sum(set(range(a, b)) <= chars for a, b, _ in gold),
        negative=not gold,
        fp_doc=not gold and bool(pred),
        fp_chars=len(chars) if not gold else 0,
        positive=bool(gold),
        failed=bool(gold) and (not pred or restored["argument"] != text),
    )
    return counts, latency


def rates(c):
    return dict(
        precision=c["tp"] / max(1, c["pred"]),
        f1=2 * c["tp"] / max(1, c["pred"] + c["gold"]),
        coverage=c["covered"] / max(1, c["gold"]),
        clean_zh_fpr=c["fp_doc"] / max(1, c["negative"]),
        clean_replaced_chars=c["fp_chars"],
        tool_restore_rate=1 - c["failed"] / max(1, c["positive"]),
    )


async def masking(rows, repeats, recognizer=None):
    recognizer = recognizer or local_pii.detector()
    counts = {}

    async def run(label):
        docs, times = zip(*(document(row, label, recognizer) for row in rows))
        counts[label] = docs
        total = Counter()
        for doc in docs:
            total.update(doc)
        metrics = rates(total)
        metrics.update(p50_ms=percentile(times, 50), p95_ms=percentile(times, 95))
        return metrics, {"restore_failures": total["failed"]} if label == LABELS[2] else {}

    exp = await experiment(
        NAMES[1],
        LABELS,
        run,
        repeats,
        "Local CPU inference adds per-document latency.",
        cache="disabled for cold-document inference",
        threshold=0.8,
    )
    rng = np.random.default_rng(42)
    indices = rng.integers(len(rows), size=(1000, len(rows)))
    intervals = {}
    for label, docs in counts.items():
        sums = {
            k: np.array([d[k] for d in docs])[indices].sum(axis=1)
            for k in ("tp", "pred", "gold", "covered")
        }
        intervals[label] = dict(
            f1=2 * sums["tp"] / np.maximum(1, sums["pred"] + sums["gold"]),
            precision=sums["tp"] / np.maximum(1, sums["pred"]),
            coverage=sums["covered"] / np.maximum(1, sums["gold"]),
        )
    exp.settings["paired_document_delta_ci95"] = {
        label: {
            k: np.percentile(v - intervals[LABELS[0]][k], [2.5, 97.5]).tolist()
            for k, v in metrics.items()
        }
        for label, metrics in intervals.items()
        if label != LABELS[0]
    }
    return exp
