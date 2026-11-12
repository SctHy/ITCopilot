"""Export reviewed examples for a human to review and commit; never commits files."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from core.db import badcases, read, shared_engine
from core.tenant_context import system_scope
from evaluation.online_regression import export_cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", choices=["promoted"], default="promoted")
    parser.add_argument("--tenant")
    parser.add_argument("--output", type=Path, default=Path("evaluation/datasets/online_regression.json"))
    args = parser.parse_args()
    engine = shared_engine(os.environ["IT_COPILOT_DATABASE_URL"])
    with system_scope(), read(engine) as db:
        query = select(badcases).where(badcases.c.status == args.status)
        if args.tenant is not None:
            query = query.where(badcases.c.tenant == args.tenant)
        rows = [dict(r) for r in db.execute(query).mappings()]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(export_cases(rows), ensure_ascii=False, indent=2) + "\n")
    print(f"exported {len(rows)} cases to {args.output}; review before committing")


if __name__ == "__main__":
    main()
