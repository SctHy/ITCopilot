"""Summarize measured quality with explicit populations and compare the preceding report."""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from core.db import (actions, approval_decisions, approvals, badcases, feedback, read,
                     shared_engine, task_events, task_verifications, turn_records)
from core.tenant_context import system_scope
from evaluation.quality import quality_report
from evaluation.reporting import REPORTS_DIR, write_report


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()


def offline_summary(paths):
    summaries = []
    for path in paths:
        data = json.loads(path.read_text())
        grouped = defaultdict(list)
        for trial in data["trials"]:
            grouped[trial["case_id"]].append(trial["passed"])
        triples = [values[:3] for values in grouped.values() if len(values) >= 3]
        singles = [passed for values in grouped.values() for passed in values]
        p1 = sum(singles) / len(singles) if singles else None
        p3 = sum(all(values) for values in triples) / len(triples) if triples else None
        summaries.append({"path": str(path), "pass_1": p1, "pass_3": p3,
                          "gap": p1 - p3 if p1 is not None and p3 is not None else None,
                          "cases": len(grouped), "pass_1_trials": len(singles), "cases_with_3_repeats": len(triples)})
    return summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, type=timestamp)
    parser.add_argument("--until", required=True, type=timestamp)
    parser.add_argument("--tenant")
    parser.add_argument("--agent-version")
    parser.add_argument("--offline-report", type=Path, action="append", default=[])
    args = parser.parse_args()
    if args.since >= args.until:
        parser.error("since must precede until")
    engine = shared_engine(os.environ["IT_COPILOT_DATABASE_URL"])
    with system_scope(), read(engine) as db:
        query = select(turn_records).where(turn_records.c.created_at >= args.since, turn_records.c.created_at < args.until)
        if args.tenant is not None:
            query = query.where(turn_records.c.tenant == args.tenant)
        if args.agent_version is not None:
            query = query.where(turn_records.c.agent_version == args.agent_version)
        rows = [dict(r) for r in db.execute(query).mappings()]
        rids = [r["request_id"] for r in rows]
        task_ids = {r["task_id"] for r in rows if r["task_id"] is not None}
        feedback_rows = [dict(r) for r in db.execute(select(feedback).where(feedback.c.request_id.in_(rids))).mappings()]
        cases = [dict(r) for r in db.execute(select(badcases).where(badcases.c.request_id.in_(rids))).mappings()]
        approval_rows = [dict(r) for r in db.execute(select(approvals).where(approvals.c.request_id.in_(rids))).mappings()]
        checks = [dict(r) for r in db.execute(select(task_verifications).where(task_verifications.c.task_id.in_(task_ids),
            task_verifications.c.ended_at >= args.since, task_verifications.c.ended_at < args.until)).mappings()]
        events = [dict(r) for r in db.execute(select(task_events).where(task_events.c.task_id.in_(task_ids),
            task_events.c.created_at >= args.since, task_events.c.created_at < args.until)).mappings()]
        unauthorized = 0
        for approval in approval_rows:
            succeeded = db.scalar(select(actions.c.id).where(actions.c.approval_id == approval["id"], actions.c.tenant == approval["tenant"],
                actions.c.kind == "apply", actions.c.status == "succeeded").limit(1))
            if succeeded is None:
                continue
            voters = list(db.scalars(select(approval_decisions.c.actor).where(approval_decisions.c.approval_id == approval["id"],
                approval_decisions.c.tenant == approval["tenant"], approval_decisions.c.decision == "approved")))
            unauthorized += int(approval["status"] != "approved" or len(set(voters)) < approval["approvals_required"] or approval["requester"] in voters)
    report = quality_report(rows, feedback_rows, cases, verifications=checks, approvals=approval_rows,
        task_events=events, unauthorized_writes=unauthorized, offline_reports=offline_summary(args.offline_report))
    report["window"] = {"since": args.since, "until": args.until, "tenant": args.tenant, "agent_version": args.agent_version}
    previous = None
    for file in sorted(REPORTS_DIR.glob("quality_*.json"), reverse=True):
        candidate = json.loads(file.read_text())
        if candidate["window"]["tenant"] == args.tenant and candidate["window"]["agent_version"] == args.agent_version:
            previous = {"path": str(file), "n": candidate["n"], "experience": candidate["experience"], "cost": candidate["cost"]}
            break
    report["previous"] = previous
    report["comparison"] = None if previous is None else {
        key: report["experience"]["latency_ms"][key] - previous["experience"]["latency_ms"][key]
        if report["experience"]["latency_ms"][key] is not None and previous["experience"]["latency_ms"][key] is not None else None
        for key in ("p50", "p95", "p99")}
    write_report("quality_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json", report)


if __name__ == "__main__":
    main()
