"""Offline P1 experiments. Each round starts with isolated workload state."""

from collections import defaultdict
import json

from .ledger import ROOT, EnvMetadata, Experiment, InvariantCheck, Report
from .stats import arm, compare, measured_rounds

NAMES = (
    "agent_guardrails_redteam",
    "masking_ablation_fpr",
    "context_prefix_cache",
    "model_router_failover",
)
FILES = dict(
    zip(NAMES, ("redteam_guardrails_v1.json", "masking_zh_eval_v1.json", "long_dialog_v1.json"))
)
DATASETS = ROOT / "evaluation/datasets"


async def experiment(name, labels, workload, repeats, trade, **settings):
    exp = Experiment(name, dict(warmup_rounds=1, **settings))
    for label in labels:
        samples = defaultdict(list)
        worst = defaultdict(float)
        for measured in measured_rounds(repeats):
            metrics, violations = await workload(label)
            for key, count in violations.items():
                worst[key] = max(worst[key], abs(count))
            if measured:
                for key, value in metrics.items():
                    samples[key].append(value)
        exp.arms.append(arm(label, samples))
        exp.checks.extend(InvariantCheck(label + "/" + k, n, 0) for k, n in worst.items())
    for treatment in exp.arms[1:]:
        for metric in treatment.metrics:
            compare(exp, exp.arms[0], treatment, metric, trade)
    return exp


async def evaluate(selected, profile, repeats, url):
    from .p1_mask import masking
    from .p1_context import context
    from .p1_guard import guardrails
    from .p1_router import router

    workloads = dict(zip(NAMES, (guardrails, masking, context, router)))
    paths = [DATASETS / FILES[n] for n in selected if n in FILES]
    report = Report(EnvMetadata.capture(profile, repeats, paths), [])
    for name in selected:
        rows = json.loads((DATASETS / FILES[name]).read_text())["cases"] if name in FILES else []
        exp = await workloads[name](rows, repeats)
        report.experiments.append(exp)
        print(name, "PASS" if exp.passed else "FAIL", flush=True)
    return report.serialize()
