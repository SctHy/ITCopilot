"""Reproducible local long-task faults. No LLM calls, network mutations, or production data."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evaluation.long_tasks import child, recovery_trial, verification_trial  # noqa: E402


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', default='/tmp/it-copilot-long-tasks.json')
    parser.add_argument('--child', choices=['crash', 'resume'], help=argparse.SUPPRESS)
    parser.add_argument('--database', help=argparse.SUPPRESS)
    parser.add_argument('--calls-path', help=argparse.SUPPRESS)
    parser.add_argument('--checkpoints', type=int, default=1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        await child(args.database, args.child, args.calls_path, bool(args.checkpoints))
        return 0
    if args.repeats < 1:
        parser.error('repeats must be positive')
    report = {'suite': 'long-task-infrastructure-v1', 'started_at': datetime.now(timezone.utc).isoformat(),
              'model': 'deterministic_specialists', 'database': 'isolated_SQLite',
              'limitations': 'Real SIGKILL, logical lease expiry, local adapters. This measures infrastructure contracts, not model intelligence, real provider cost, or production availability.',
              'trials': []}
    with tempfile.TemporaryDirectory(prefix='it-long-tasks-') as root:
        for repeat in range(args.repeats):
            for scenario in ('recovery_baseline', 'recovery_checkpoints', 'false_completion', 'actual_entitlement', 'unavailable_verifier'):
                directory = Path(root) / f'{repeat}-{scenario}'
                directory.mkdir()
                if scenario.startswith('recovery_'):
                    row = await recovery_trial(directory, scenario == 'recovery_checkpoints')
                else:
                    row = await verification_trial(directory, scenario)
                row['repeat'] = repeat + 1
                report['trials'].append(row)
                print(f"{scenario} {repeat + 1}: {'PASS' if row['passed'] else 'FAIL'}", flush=True)
    report['passed'], report['total'] = sum(r['passed'] for r in report['trials']), len(report['trials'])
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f"{report['passed']}/{report['total']} {args.output}")
    return 0 if report['passed'] == report['total'] else 1


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
