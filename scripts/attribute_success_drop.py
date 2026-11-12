"""Read persistent metadata and compare two non-overlapping success windows."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from core.db import change_events, read, shared_engine, turn_records
from core.tenant_context import system_scope
from evaluation.attribution import attribute, markdown
from evaluation.reporting import write_report


def window(value):
    start, end = value.split("/")
    def stamp(text):
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            raise ValueError("窗口时间须带时区，例如 2026-11-01T00:00+08:00")
        return parsed.timestamp()
    result = stamp(start), stamp(end)
    if result[0] >= result[1]:
        raise ValueError("窗口起点必须早于终点")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, type=window)
    parser.add_argument("--current", required=True, type=window)
    parser.add_argument("--tenant")
    parser.add_argument("--channel")
    args = parser.parse_args()
    if args.baseline[1] > args.current[0]:
        parser.error("baseline 必须早于 current，且不得重叠")
    engine = shared_engine(os.environ["IT_COPILOT_DATABASE_URL"])
    with system_scope(), read(engine) as db:
        groups = []
        for start, end in (args.baseline, args.current):
            stmt = select(turn_records).where(turn_records.c.created_at >= start, turn_records.c.created_at < end)
            if args.tenant:
                stmt = stmt.where(turn_records.c.tenant == args.tenant)
            if args.channel:
                stmt = stmt.where(turn_records.c.channel == args.channel)
            groups.append([dict(row) for row in db.execute(stmt.order_by(turn_records.c.created_at)).mappings()])
        stmt = select(change_events).where(change_events.c.at >= args.baseline[0], change_events.c.at < args.current[1])
        if args.tenant:
            stmt = stmt.where((change_events.c.tenant == args.tenant) | change_events.c.tenant.is_(None))
        events = [dict(row) for row in db.execute(stmt.order_by(change_events.c.at)).mappings()]
    report = attribute(*groups, events)
    report["windows"] = {"baseline": args.baseline, "current": args.current}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = write_report(f"attribution_{stamp}.json", report, echo=False)
    print(markdown(report))
    print(f"\nJSON: {path}")


if __name__ == "__main__":
    main()
