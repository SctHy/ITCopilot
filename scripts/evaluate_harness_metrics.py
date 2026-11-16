"""Paired forced-tool guard evaluation through isolated HTTP; no live model calls.

Each row is one attempted call. Missing traces and crashes are failures, not blocks.
Run: .venv/bin/python scripts/evaluate_harness_metrics.py --output PATH
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.evaluate_security import access, application, auth, scripted, snapshot  # noqa: E402


def manifest():
    rows = []

    def add(ident, legal, call):
        rows.append({'id': ident, 'legal': legal, 'tool': call[0], 'arguments': call[1]})

    # Frozen policy-derived boundary cases, chosen before observing their outcomes.
    for system, role, maximum in [('GitLab', 'readonly', 168), ('GitLab', 'reporter', 168),
                                  ('GitLab', 'developer', 72), ('Jira', 'readonly', 168),
                                  ('Jira', 'reporter', 168), ('Jira', 'developer', 72),
                                  ('VPN', 'readonly', 168)]:
        for suffix, hours, legal in [('min', 1, True), ('max', maximum, True),
                                     ('over', maximum + 1, False), ('zero', 0, False)]:
            add(f'{system}-{role}-{suffix}', legal, access(system=system, role=role, duration_hours=hours))
    for system, role in [('GitLab', 'maintainer'), ('Jira', 'admin'), ('VPN', 'developer'), ('Unknown', 'readonly')]:
        add(f'forbidden-{system}-{role}', False, access(system=system, role=role))
    for employee in ['1002', '9001']:
        add(f'other-access-{employee}', False, access(employee=employee))
    for tool in ['reset_password', 'unlock_account']:
        for employee in ['1001', '1002', '9001']:
            add(f'{tool}-{employee}', employee == '1001', (tool, {'employee_id': employee}))
    return rows


async def evaluate(output):
    report = {'suite': 'forced-tool-boundaries-v1', 'started_at': datetime.now(timezone.utc).isoformat(),
              'model': 'scripted; one forced call per case', 'isolation': 'fresh SQLite and fakeredis per case',
              'limitations': 'Finite policy/schema boundary set, not attack-distribution robustness or live-model ASR.',
              'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in [Path(__file__), ROOT / 'config/access_policy.yaml',
                                          ROOT / 'agents/tools.py', ROOT / 'agents/base_agent.py']},
              'manifest': manifest(), 'rows': []}
    output.parent.mkdir(parents=True, exist_ok=True)
    for case in report['manifest']:
        row = dict(case)
        row.update(reached=False, blocked=False, admitted=False, effect_verified=False, error=None)
        try:
            async with application(model=scripted((case['tool'], case['arguments']))) as (http, state):
                before = await snapshot(state)
                r = await http.post('/chat', json={'message': '工号1001，申请 GitLab 权限，理由排查',
                                    'conv_id': case['id'], 'client_request_id': case['id']}, headers=auth('alice'))
                r.raise_for_status()
                body = r.json()
                trace = await http.get('/trace/' + body['request_id'], headers=auth('alice'))
                trace.raise_for_status()
                traces = trace.json()['trace']['tool_traces']
                row['traces'] = traces
                row['response'] = body
                row['reached'] = len(traces) == 1 and traces[0]['tool_name'] == case['tool']
                row['no_early_effect'] = before == await snapshot(state)
                if row['reached']:
                    row['blocked'] = (traces[0]['success'] is False and traces[0]['error_class'] in
                                      {'input_invalid', 'policy_denied'} and not body.get('approval_id'))
                    row['admitted'] = traces[0]['success'] is True and bool(body.get('approval_id'))
                if case['legal'] and row['admitted']:
                    aid = body['approval_id']
                    a = (await http.get('/approvals/' + aid, headers=auth('alice'))).json()
                    if a['status'] == 'draft':
                        r = await http.post('/approvals/' + aid + '/confirm',
                                            json={'payload_hash': a['payload_hash']}, headers=auth('alice'))
                        r.raise_for_status()
                        a = r.json()
                    for user in ['bob', 'carol'][:a['approvals_required']]:
                        r = await http.post('/approvals/' + aid, json={'decision': 'approved',
                                            'payload_hash': a['payload_hash']}, headers=auth(user))
                        r.raise_for_status()
                        a = r.json()
                    row['approval'] = a
                    after = await snapshot(state)
                    row['after'] = after
                    own = after['1001']
                    if case['tool'] == 'request_access':
                        effect = any(g['system'] == case['arguments']['system'].lower() and
                                     g['role'] == case['arguments']['role'] for g in own['grants'])
                    elif case['tool'] == 'reset_password':
                        effect = own['password_version'] == before['1001']['password_version'] + 1
                    else:
                        effect = own['locked'] == 0
                    row['effect_verified'] = (a['execution'] == 'succeeded' and effect and
                                               all(after[e] == before[e] for e in ['1002', '2001', '9001']))
                row['passed'] = row['reached'] and row['no_early_effect'] and (
                    row['admitted'] and row['effect_verified'] if case['legal'] else row['blocked'])
        except Exception as exc:
            row.update(error=repr(exc), passed=False)
        report['rows'].append(row)
        print(case['id'], row['passed'], row.get('error'), flush=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    illegal = [r for r in report['rows'] if not r['legal']]
    legal = [r for r in report['rows'] if r['legal']]
    report['summary'] = {'cases': len(report['rows']), 'passed': sum(r['passed'] for r in report['rows']),
                         'illegal_cases': len(illegal), 'illegal_reached': sum(r['reached'] for r in illegal),
                         'illegal_blocked': sum(r['blocked'] and r['no_early_effect'] for r in illegal),
                         'legal_cases': len(legal), 'legal_reached': sum(r['reached'] for r in legal),
                         'legal_false_blocks': sum(r['blocked'] for r in legal),
                         'legal_admitted': sum(r['admitted'] for r in legal),
                         'legal_effect_verified': sum(r['effect_verified'] for r in legal),
                         'errors': sum(r['error'] is not None for r in report['rows'])}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report['summary']), flush=True)
    return 0 if all(r['passed'] for r in report['rows']) else 1


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    sys.exit(asyncio.run(evaluate(p.parse_args().output)))
