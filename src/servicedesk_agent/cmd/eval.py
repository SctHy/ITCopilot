"""Run infrastructure and agent ablations, compare ledgers, or render an offline report."""

import argparse
import asyncio
from collections import Counter, defaultdict
import json
from pathlib import Path
import tempfile
from datetime import UTC, datetime

from servicedesk_agent.cmd.bench import database, positive
from servicedesk_agent.eval.ledger import DATA, ROOT, EnvMetadata, Experiment, InvariantCheck, Report
from servicedesk_agent.eval.rag import rag
from servicedesk_agent.eval.stats import arm, measured_rounds, compare
from servicedesk_agent.eval.workloads import fair, fault, notify
from servicedesk_agent.workflow.store import WorkflowStore

NAMES = (
    "rag_hybrid_ablation",
    "workflow_notify_vs_poll",
    "fair_queue_noisy_neighbor",
    "fault_observe_before_retry",
)


async def workflow_experiment(name, profile, repeats, url):
    if name == NAMES[1]:
        labels = ("poll_only_baseline", "pg_notify_prod" if url else "local_kick_prod")
        settings = dict(
            baseline_poll_s=1 if profile == "quick" else 5,
            prod_poll_s=5,
            empty_claims_per_s="includes drain attempts, idle_empty_claims_per_s excludes work",
        )
        trade = "Realtime wakeups require a listener and can add empty claims after bursts."
        workload = notify
        configurations = [("", (profile,))]
    elif name == NAMES[2]:
        labels = ("fifo_unprotected", "fair_protected_prod")
        settings = dict(backlog=160, quiet=8, window_first_completions=16)
        trade = "Serving the quiet tenant earlier displaces noisy-tenant throughput in the window."
        workload = fair
        configurations = [(f"/workers={n}", (n,)) for n in (1, 2)]
    else:
        labels = ("blind_retry_baseline", "observe_leased_prod")
        cases = 5 if profile == "quick" else 15
        settings = dict(
            cases_per_scene=cases,
            duplicate_rate_denominator="all injected cases",
            drift_consistency="ledger drifted and external change preserved",
            adapter="in-process non-idempotent MemoryTarget",
        )
        trade = "Observe adds read latency per case; drift requires operator review."
        workload = fault
        configurations = [("", (cases,))]

    exp = Experiment(name, settings)
    for suffix, params in configurations:
        arms = []
        for protected, label in enumerate(labels):
            measures = defaultdict(list)
            checks = Counter()
            for measured in measured_rounds(repeats):
                with tempfile.TemporaryDirectory() as folder, database(Path(folder), url) as db:
                    store = WorkflowStore(db)
                    try:
                        metrics, gates = await workload(store, bool(protected), *params)
                    finally:
                        await store.aclose()
                checks.update(gates)
                if measured:
                    for key, value in metrics.items():
                        measures[key].append(value)
            arms.append(arm(label + suffix, measures))
            if protected or name != NAMES[3]:
                exp.checks.extend(
                    InvariantCheck(label + suffix + "/" + k, v, 0) for k, v in checks.items()
                )
        exp.arms.extend(arms)
        for key in arms[0].metrics:
            compare(exp, *arms, key, trade, paired=False)
    return exp


async def evaluate(selected, profile, repeats, url):
    report = Report(
        EnvMetadata.capture(profile, repeats, [DATA] if NAMES[0] in selected else []), []
    )
    backend = "postgresql" if url else "sqlite"
    print("Experiment                         Backend      Gate")
    for name in selected:
        if name == NAMES[0]:
            with tempfile.TemporaryDirectory() as folder, database(Path(folder), url) as db:
                exp = rag(db, profile, repeats)
        else:
            exp = await workflow_experiment(name, profile, repeats, url)
        exp.settings.update(backend=backend, warmup_rounds=1)
        report.experiments.append(exp)
        print(f"{name:<34} {backend:<12} {'PASS' if exp.passed else 'FAIL'}", flush=True)
        for delta in exp.deltas:
            if delta.metric.startswith(("exact_code/", "semantic_paraphrase/", "composite_query/")):
                continue
            if (
                delta.metric.startswith("dept_isolation/")
                and delta.treatment != "sql_prefilter_prod"
            ):
                continue
            base, prod = (
                next(a for a in exp.arms if a.name == label)
                for label in (delta.baseline, delta.treatment)
            )
            print(
                f"  {delta.baseline} → {delta.treatment}   {delta.metric:<24} "
                f"{base.metrics[delta.metric].mean:.4g} → {prod.metrics[delta.metric].mean:.4g}"
            )
    return report.serialize()


async def evaluate_all(selected, profile, repeats, url):
    from servicedesk_agent.eval import p1

    suites = []
    for names, runner, backend in ((NAMES, evaluate, url), (p1.NAMES, p1.evaluate, None)):
        subset = [name for name in names if name in selected]
        if subset:
            suites.append(await runner(subset, profile, repeats, backend))
    return {
        "env": {
            **suites[0]["env"],
            "datasets": [data for suite in suites for data in suite["env"]["datasets"]],
        },
        "experiments": [exp for suite in suites for exp in suite["experiments"]],
        "passed": all(suite["passed"] for suite in suites),
    }


def postprocess(args):
    from servicedesk_agent.eval.diff import compare_ledgers, terminal
    from servicedesk_agent.eval.render import render

    inputs = [args.baseline] if args.baseline else []
    if args.suite == "diff":
        inputs.append(args.candidate)
        outputs = [args.out] if args.out else []
    else:
        inputs.append(args.ledger)
        outputs = [p for p in (args.md, args.html) if p]
    paths = [p.resolve() for p in outputs]
    if (
        len(set(paths)) != len(paths)
        or set(paths).intersection(p.resolve() for p in inputs)
        or any(p.exists() and p.is_relative_to(ROOT / "evaluation/reports") for p in paths)
    ):
        raise ValueError("choose distinct output paths outside historical reports and inputs")
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    if args.suite == "diff":
        result = compare_ledgers(
            baseline, json.loads(args.candidate.read_text()), args.threshold, args.allow_missing
        )
        payload = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if args.out:
            args.out.write_text(payload + "\n")
        print(payload if args.json else terminal(result))
        return 0 if result["passed"] else 1
    ledger = json.loads(args.ledger.read_text())
    if args.md or not args.html:
        markdown = render(ledger, baseline)
        if args.md:
            args.md.write_text(markdown)
        else:
            print(markdown, end="")
    if args.html:
        args.html.write_text(render(ledger, baseline, html=True))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="suite", required=True)
    for name in ("p0", "p1", "all"):
        run = sub.add_parser(name)
        run.add_argument("--profile", choices=["quick", "full"], default="quick")
        run.add_argument("--repeats", type=positive)
        run.add_argument("--database-url")
        run.add_argument("--only")
        run.add_argument("--out", type=Path)
    diff = sub.add_parser("diff")
    diff.add_argument("--baseline", type=Path, required=True)
    diff.add_argument("--candidate", type=Path, required=True)
    diff.add_argument("--threshold", type=float, default=0.15)
    diff.add_argument("--allow-missing", action="store_true")
    diff.add_argument("--json", action="store_true")
    diff.add_argument("--out", type=Path)
    report = sub.add_parser("report")
    report.add_argument("--in", dest="ledger", type=Path, required=True)
    report.add_argument("--baseline", type=Path)
    report.add_argument("--md", type=Path)
    report.add_argument("--html", type=Path)
    abc = sub.add_parser("devsuite-abc")
    abc.add_argument("--driver", choices=("offline", "live"), default="offline")
    abc.add_argument("--repeats", type=positive, default=3)
    abc.add_argument("--concurrency", type=positive, default=6)
    abc.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    if args.suite == "devsuite-abc":
        from servicedesk_agent.eval.devsuite_abc import command
        return command(args)
    if args.suite in ("diff", "report"):
        try:
            return postprocess(args)
        except (ValueError, OSError, KeyError) as exc:
            parser.error(str(exc))
    names, runner = NAMES, evaluate
    if args.suite == "all":
        from servicedesk_agent.eval import p1

        names, runner = NAMES + p1.NAMES, evaluate_all
    elif args.suite == "p1":
        from servicedesk_agent.eval import p1

        names, runner = p1.NAMES, p1.evaluate
        if args.database_url:
            parser.error("p1 uses disposable local workloads")
    selected = list(names) if args.only is None else args.only.split(",")
    if set(selected) - set(names) or len(set(selected)) != len(selected):
        parser.error("--only requires unique names from " + ", ".join(names))
    out = args.out or Path("evaluation/reports") / datetime.now(UTC).strftime(
        f"{args.suite}_%Y%m%d_%H%M%S_%f.json"
    )
    if out.exists():
        parser.error("output path already exists")
    result = asyncio.run(
        runner(
            selected,
            args.profile,
            args.repeats or (3 if args.profile == "quick" else 5),
            args.database_url,
        )
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("x") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, allow_nan=False)
    print(out)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
