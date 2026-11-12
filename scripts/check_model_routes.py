"""CI离线检查：每个候选必须有真实评测报告，缺报告或不满足Wilson下界门槛时失败。"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.model_router import DEFAULT_CONFIG, RoutingConfig
from evaluation.attribution import wilson
import yaml


def check(config_path=DEFAULT_CONFIG, reports=ROOT / "evaluation/reports/routing") -> list[str]:
    config = RoutingConfig.model_validate(yaml.safe_load(pathlib.Path(config_path).read_text()))
    errors = []
    for name, route in config.routes.items():
        measured = {}
        for candidate in route.candidates:
            path = pathlib.Path(reports) / f"{name}__{candidate}.json"
            if not path.exists():
                errors.append(f"{name}/{candidate}: missing report {path}")
                continue
            report = json.loads(path.read_text())
            if (report["route"] != name or report["model"] != candidate or report["status"] != "complete"
                    or report["repeats"] < 3 or report["trials"] == 0 or report["passed"] == 0 or report["kind"] != "real_model" or report["route_calls"] == 0):
                errors.append(f"{name}/{candidate}: incomplete or non-live evaluation")
                continue
            fingerprint = hashlib.sha256(json.dumps(config.models[candidate].model_dump(), sort_keys=True).encode()).hexdigest()
            if report["model_config_sha256"] != fingerprint:
                errors.append(f"{name}/{candidate}: report belongs to a different model configuration")
                continue
            measured[candidate] = (wilson(report["passed"], report["trials"])[0], report["dataset_sha256"])
        primary = route.candidates[0]
        if primary in measured:
            primary_lower, primary_dataset = measured[primary]
            for candidate, (lower, dataset) in measured.items():
                if dataset != primary_dataset:
                    errors.append(f"{name}/{candidate}: candidate and primary used different datasets")
                elif lower < primary_lower - .03:
                    errors.append(f"{name}/{candidate}: Wilson lower bound {lower:.4f} < primary {primary_lower:.4f} - .03")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    parser.add_argument("--reports", default=ROOT / "evaluation/reports/routing")
    args = parser.parse_args()
    errors = check(args.config, args.reports)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        raise SystemExit(1)
    print("All configured route candidates passed the evaluation gate.")


if __name__ == "__main__":
    main()
