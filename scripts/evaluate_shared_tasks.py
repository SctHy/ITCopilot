"""Shared-task HTTP evaluation with isolated business data; --live enables real model calls."""
import argparse
import asyncio
import json
import pathlib
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv  # noqa: E402
from evaluation.application import application  # noqa: E402
from evaluation.shared_tasks import CASES, offline_handler, run_case  # noqa: E402


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--cases', default=','.join(CASES))
    parser.add_argument('--redis-url')
    parser.add_argument('--output', default='/tmp/it-copilot-shared-evaluation.json')
    args = parser.parse_args()
    if args.repeats < 1 or any(c not in CASES for c in args.cases.split(',')):
        parser.error('repeats must be positive and cases must be known')
    if args.live:
        load_dotenv(pathlib.Path(__file__).resolve().parents[1] / '.env')
    report = {'suite': 'shared-tasks-product-v2', 'structured_progress_required': args.live, 'model_mode': 'live' if args.live else 'deterministic_handler',
              'model': os.getenv('OPENAI_MODEL', 'deepseek-chat') if args.live else None,
              'transport': 'in_process_HTTP', 'database': 'isolated_SQLite', 'started_at': datetime.now(timezone.utc).isoformat(),
              'limitations': 'HTTP identity sessions, not browser automation. Business adapters are local. Unknown model usage is not priced.', 'trials': []}
    async with application(redis_url=args.redis_url) as (http, state):
        if not args.live:
            state.task_worker.handle = offline_handler
        for case in args.cases.split(','):
            for repeat in range(args.repeats):
                row = await run_case(http, state, case, require_suggestions=args.live)
                row['repeat'] = repeat + 1
                report['trials'].append(row)
                print(f"{case} {repeat + 1}: {'PASS' if row['passed'] else 'FAIL'}", flush=True)
                report['passed'] = sum(r['passed'] for r in report['trials'])
                report['total'] = len(report['trials'])
                pathlib.Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f"{report['passed']}/{report['total']} {args.output}")
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
