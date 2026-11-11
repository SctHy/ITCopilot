"""Copy legacy long-term memory using operator-verified subject->tenant bindings."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.db import shared_engine  # noqa: E402
from memory.scope_migration import migrate_legacy  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-url', default=os.getenv('IT_COPILOT_DATABASE_URL'))
    parser.add_argument('--bindings', required=True, help='JSON array of verified {subject, tenant} records')
    parser.add_argument('--apply', action='store_true', help='Without this flag, only report counts')
    args = parser.parse_args()
    if not args.database_url:
        parser.error('set --database-url or IT_COPILOT_DATABASE_URL')
    engine = shared_engine(args.database_url)
    try:
        report = migrate_legacy(engine, json.loads(Path(args.bindings).read_text()), apply=args.apply)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
