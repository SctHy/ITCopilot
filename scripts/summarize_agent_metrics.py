"""Audit saved live trials and explicit semantic-review labels; does not call a model.

Semantic labels below are an assistant review of the frozen 45-response journey run,
not a keyword classifier or independent human annotation. Other input hashes rejected.
"""
import argparse
from datetime import datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from evaluation.frozen import frozen_input  # noqa: E402


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def percentile(values, p):
    values = sorted(values)
    i = (len(values) - 1) * p
    return values[math.floor(i)] + (values[math.ceil(i)] - values[math.floor(i)]) * (i % 1)


def audit(output):
    output.mkdir(parents=True, exist_ok=True)
    paths = {n: frozen_input('application-' + n) for n in ['core', 'holdout', 'mixed', 'journeys']}
    sources = {n: json.loads(p.read_text()) for n, p in paths.items()}
    performance = {'evidence': 'Recomputed from prior live DeepSeek trials; not a new live-model run.',
                   'latency_definition': 'Whole scenario wall time, including multi-turn chat and automated approval/check HTTP calls; no human waiting or public network load.',
                   'percentile_method': 'linear interpolation, index (n-1)*p',
                   'cost_definition': 'All intent+agent provider costs, failures included, divided by original scorer successes. Saved peak/off-peak price assumptions; not an invoice.',
                   'scoring_limit': 'Original scores retained. Known stale assertions: five employee-ID clarifications, compound approval count, context-switch historical approval. Do not advertise this as adjudicated business success.',
                   'source_sha256': {n: digest(paths[n]) for n in sources}, 'suites': {}}
    all_rows = []
    for name in ['core', 'holdout', 'mixed', 'pooled']:
        rows = sources[name]['trials'] if name != 'pooled' else all_rows
        if name != 'pooled':
            all_rows.extend(rows)
        passed = sum(r['passed'] for r in rows)
        costs = {period: sum(r['model_usage']['cost_usd_' + period] for r in rows) for period in ['peak', 'off_peak']}
        performance['suites'][name] = {'trials': len(rows), 'original_scorer_passed': passed,
            'turns': sum(len(r['turns']) for r in rows), 'provider_calls': sum(r['model_usage']['calls'] for r in rows),
            'tokens': {k: sum(r['model_usage']['usage'][k] for r in rows) for k in ['prompt', 'hit', 'miss', 'output']},
            'p50_s': percentile([r['latency_s'] for r in rows], .5),
            'p95_s': percentile([r['latency_s'] for r in rows], .95),
            'successful_p95_s': percentile([r['latency_s'] for r in rows if r['passed']], .95),
            'total_estimated_usd': costs,
            'estimated_usd_per_original_scored_success': {k: v / passed for k, v in costs.items()}}
    (output / 'performance.json').write_text(json.dumps(performance, ensure_ascii=False, indent=2) + '\n')

    assert digest(paths['journeys']) == 'c7131a0288688b2b286174c1e8ca164518ac03d0be42356085168f275bd2c5b9', 'Semantic labels only apply to the frozen reviewed run'
    journeys = sources['journeys']
    # Fixed annotation scope: all responses in the named frozen run, including one deterministic confirmation.
    stages = {
        'password': ['draft', 'draft', 'pending', 'executed', 'executed', 'executed'],
        'withdraw': ['draft', 'draft', 'pending', 'withdrawn', 'withdrawn'],
        'reject': ['draft', 'draft', 'pending', 'rejected', 'rejected'],
        'shared': ['read_only'] * 4 + ['draft', 'pending', 'executed', 'executed'],
        'access_lifecycle': ['draft', 'draft', 'pending', 'one_vote', 'executed', 'released', 'released'],
        'access_unconfirmed': ['draft', 'draft', 'pending', 'one_vote', 'executed', 'executed', 'released', 'released'],
        'typed_withdraw': ['draft', 'draft', 'pending', 'pending', 'withdrawn', 'withdrawn'],
    }
    review = {'reviewer': 'Codex assistant, single full-text review; no independent human agreement measured',
              'scope': 'Application status and claimed side effects in 7 frozen journeys, 45 responses (44 model replies + 1 deterministic confirmation). Not all answer factuality or knowledge citations.',
              'rubric': {'false_completion': 'Says business change executed when corresponding action has not executed. Creating a draft is not completion.',
                         'status_error': 'A concrete current/historical approval state or entitlement expiry contradicts recorded scenario/API facts.',
                         'ambiguous': 'Wording permits both a correct and incorrect reading; report separately.'},
              'source_sha256': digest(paths['journeys']), 'rows': []}
    for case in journeys['cases']:
        assert case['passed'] and all(case['checks'].values())
        assert len(case['turns']) == len(stages[case['id']])
        for i, (turn, stage) in enumerate(zip(case['turns'], stages[case['id']]), 1):
            row = {'case': case['id'], 'turn': i, 'expected_stage': stage,
                   'response': turn['response'].get('response', turn['response'].get('text')),
                   'false_completion': False, 'status_error': False, 'ambiguous': False,
                   'basis': 'Scenario action order and persisted checks in evaluate_role_journeys.py; final approval and business state.'}
            if (case['id'], i) == ('typed_withdraw', 5):
                row.update(status_error=True, basis='Says 未进入审批, but turn 3 confirmed and confirmed_pending check passed before withdrawal.')
            if (case['id'], i) == ('typed_withdraw', 6):
                row.update(ambiguous=True, basis='审批前被撤回 can mean before entering approval (wrong) or before approval decision (correct).')
            if (case['id'], i) == ('access_lifecycle', 5):
                approved = datetime.fromisoformat(case['final_approval']['decided_at'])
                expected = approved + timedelta(hours=24)
                assert approved.date().isoformat() == '2026-11-13' and '2026-11-15 19:13' in row['response']
                row.update(status_error=True, basis=f'Claims expiry 2026-11-15 19:13 for 24h grant approved at {approved.isoformat()}; expected approximately {expected.isoformat()}. Release later overwrote expiry, so this is reconstructed from approval time+duration, not a preserved pre-release snapshot.')
            review['rows'].append(row)
    at_risk = [r for r in review['rows'] if r['expected_stage'] in {'draft', 'pending', 'one_vote', 'withdrawn', 'rejected', 'read_only'}]
    review['summary'] = {'responses_reviewed': len(review['rows']), 'status_error_responses': sum(r['status_error'] for r in review['rows']),
                         'ambiguous_responses': sum(r['ambiguous'] for r in review['rows']),
                         'pre_execution_responses': len(at_risk), 'false_completion_responses': sum(r['false_completion'] for r in at_risk)}
    assert len(review['rows']) == 45
    (output / 'semantic-review.json').write_text(json.dumps(review, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'performance': performance['suites'], 'semantic': review['summary']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    audit(parser.parse_args().output)
