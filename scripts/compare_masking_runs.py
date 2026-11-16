"""Paired document bootstrap for two completed masking runs; never mix corpora."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

import numpy as np


def read(path):
    path = Path(path)
    report = json.loads(path.read_text())
    if "modes" in report:
        overall = report["modes"]["on"]["overall"]
        records = report["modes"]["on"].get("documents")
    else:
        overall, records = report["overall"], None
    if records is None:
        records = [r for r in map(json.loads, path.with_name("documents.jsonl").read_text().splitlines())
                   if r.get("mode", "on") == "on"]
    # Some external corpora reuse their UID across different texts. The original
    # file row index remains unique; never silently discard rows in a dict.
    keyed = {r.get("source_index", r["id"]): r for r in records}
    assert len(keyed) == len(records), "Duplicate document identities"
    return overall, keyed


def coverage_changes(before, after):
    """Report gains and losses separately so net improvement cannot hide leaks."""
    assert before.keys() == after.keys(), "Documents differ"
    by_type = defaultdict(Counter)
    for key, old in before.items():
        new = after[key]
        assert old["gold_spans"] == new["gold_spans"], "Gold labels changed"
        for (_, _, kind), was, now in zip(old["gold_spans"], old["full_flags"], new["full_flags"], strict=True):
            row = by_type[kind]
            row["gold"] += 1
            row["before_fully_covered"] += bool(was)
            row["after_fully_covered"] += bool(now)
            row["gained"] += not was and now
            row["lost"] += was and not now
    return dict(by_type)


def compare(before, after):
    old, a = read(before)
    new, b = read(after)
    assert a.keys() == b.keys(), "Documents differ"
    for key in a:
        assert a[key]["gold_spans"] == b[key]["gold_spans"], "Gold labels changed"
        assert a[key].get("hash", a[key].get("text")) == b[key].get("hash", b[key].get("text")), "Input changed"
    ids = sorted(a)
    fields = ("tp", "gold", "predicted", "fully_covered")
    x, y = [np.array([[rows[i][k] for k in fields] for i in ids]) for rows in (a, b)]
    rng = np.random.default_rng(20261114)
    changes = []
    for _ in range(1000):
        selected = rng.integers(0, len(ids), len(ids))
        s, t = x[selected].sum(axis=0), y[selected].sum(axis=0)
        changes.append([2*t[0]/(t[1]+t[2])-2*s[0]/(s[1]+s[2]), t[3]/t[1]-s[3]/s[1]])
    ci = np.quantile(changes, [.025, .975], axis=0) * 100
    return {"before_file": str(before), "after_file": str(after), "paired_documents": len(ids),
            "before": old, "after": new,
            "coverage_changes_by_type": coverage_changes(a, b),
            "delta_percentage_points": {k: (100*(new[k]-old[k]) if new[k] is not None and old[k] is not None else None)
                                        for k in ("f1", "full_coverage_rate", "precision", "recall", "negative_change_rate")},
            "paired_bootstrap_95ci_pp": {"f1": ci[:, 0].tolist(), "full_coverage": ci[:, 1].tolist()},
            "notes": "Document-level resampling, conditional on the synthetic corpus; no production accuracy claim. Timing not comparable when runs share CPU."}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--before", required=True)
    p.add_argument("--after", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()
    result = compare(args.before, args.after)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in {"before", "after"}}, indent=2))
