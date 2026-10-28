#!/usr/bin/env python3
"""Fit source-bound temperatures using ONLY calibration rows of a saved v2 report.

No model calls. --install is explicit; small held-out results do not imply that a
fit generalizes to a different model or embedding provider.
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from core.intent_calibration import fit_temperatures
from evaluation.reporting import ROOT, REPORTS_DIR, write_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=str(REPORTS_DIR / "intent_ablation_live.json"))
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args()
    report = json.loads(pathlib.Path(args.report).read_text())
    if report["status"] != "complete":
        raise ValueError("Incomplete source predictions cannot calibrate the runtime")
    temperatures = {}
    for strategy in report["strategies"].values():
        rows = [r for r in strategy["cases"] if r["split"] == "calibration"]
        for source in {r["source"] for r in rows}:
            groups = {}
            for row in rows:
                if row["source"] == source:
                    groups.setdefault(row["group"], []).append((row["confidence"], row["correct"]))
            temperatures[source] = fit_temperatures(groups)
    config = {
        "schema_version": 2,
        "fit_split": "calibration",
        "dataset_sha256": report["dataset_sha256"],
        "temperatures_by_source": temperatures,
    }
    write_report("calibration_candidate_v2.json", config, echo=False)
    if args.install:
        (ROOT / "evaluation/config/intent_calibration.json").write_text(
            json.dumps(config, indent=2)
        )
    print("Candidate written; installed=" + str(args.install))


if __name__ == "__main__":
    main()
