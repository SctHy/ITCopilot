"""Recompute resume metrics from saved per-query evidence; does not rerun retrieval.

Usage: .venv/bin/python scripts/audit_resume_metrics.py --output PATH
Historical benchmark scores are kept separate from current safety regression results.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.evaluate_rag_wixqa import load, sha256  # noqa: E402
from evaluation.frozen import frozen_input  # noqa: E402


def recall(row):
    gold = set(row['gold'])
    return len(gold & set(row['retrieved'][:5])) / len(gold)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    paths = [frozen_input(name) for name in ('rag-base', 'rag-rerank')]
    base, rerank = [json.loads(p.read_text()) for p in paths]
    assert base['embedding_model'] == rerank['embedding_model']
    assert base['chunk_window'] == rerank['candidates'] == 20
    corpus = load('corpus')  # pinned checksum, no network
    pooled = {'vector': [], 'hybrid': [], 'hybrid_llm_rerank': []}
    suites = {}
    paired = []
    for name in ('expertwritten', 'simulated'):
        queries = load(name)
        rows = {strategy: sorted(report['cases'], key=lambda x: x['id']) for strategy, report in (
            ('vector', base['strategies']['vector'][name]),
            ('hybrid', base['strategies']['hybrid'][name]),
            ('hybrid_llm_rerank', rerank['strategies']['llm@200'][name]))}
        old_hybrid = sorted(rerank['strategies']['hybrid'][name]['cases'], key=lambda x: x['id'])
        assert len(old_hybrid) == len(queries) == 200
        assert all(len(v) == 200 for v in rows.values())
        for i, (v, h, rr, oh) in enumerate(zip(rows['vector'], rows['hybrid'], rows['hybrid_llm_rerank'], old_hybrid)):
            assert v['id'] == h['id'] == rr['id'] == oh['id'] == f'{name}-{i:03d}'
            assert set(v['gold']) == set(h['gold']) == set(rr['gold']) == set(queries[i]['article_ids'])
            assert h['retrieved'] == oh['retrieved']  # same recorded baseline across the two experiments
            for row in (v, h, rr):
                assert abs(recall(row) - row['recall_at_5']) < 1e-10
            paired.append({'id': v['id'], **{k: recall(rows[k][i]) for k in rows}})
        suites[name] = {}
        for strategy, values in rows.items():
            scores = [recall(row) for row in values]
            pooled[strategy].extend(scores)
            suites[name][strategy] = {'n': len(scores), 'recall_at_5': float(np.mean(scores)),
                'hit_at_5': float(np.mean([x > 0 for x in scores]))}
    means = {k: float(np.mean(v)) for k, v in pooled.items()}
    delta = np.array(pooled['hybrid_llm_rerank']) - np.array(pooled['vector'])
    rng = np.random.default_rng(20261114)
    boot = delta[rng.integers(0, len(delta), size=(5000, len(delta)))].mean(axis=1)
    result = {'evidence_type': 'historical per-query artifacts recomputed, not a fresh retrieval run',
        'source_sha256': {str(p.relative_to(ROOT)): sha256(p) for p in paths},
        'source': base['source'], 'articles': len(corpus), 'queries': len(delta),
        'embedding_model': base['embedding_model'], 'reranker': rerank['llm_model'],
        'scoring': 'macro article Recall@5: deduplicate article IDs from ranked chunks; not answer accuracy',
        'candidates': 20, 'rerank_returned_chunks': rerank['rerank_top'],
        'rerank_failed': sum(rerank['strategies']['llm@200'][n]['rerank_failed'] for n in suites),
        'means': means, 'by_suite': suites,
        'improvement_pp': (means['hybrid_llm_rerank'] - means['vector']) * 100,
        'relative_improvement_percent': (means['hybrid_llm_rerank'] / means['vector'] - 1) * 100,
        'paired_query_bootstrap_delta_pp_95ci': (np.quantile(boot, [.025, .975]) * 100).tolist(),
        'bootstrap_limit': 'query resampling only; does not include model rerun variance',
        'paired': paired,
        'limits': ['English Wix support corpus, not private Chinese enterprise documents.',
                   'Saved rerank run has no original executable command or code fingerprint; current performance not asserted.',
                   'Reported retrieval configurations differ in returned chunk count (20 baseline, 10 reranked); metric is article-level top 5.',
                   'Rerank is optional and disabled by default; enabling it adds model latency.']}
    (output/'rag-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    attacks = []
    for suite in ('workspace','travel','banking','slack'):
        p=frozen_input('agentdojo-' + suite)
        d=json.loads(p.read_text()); attacks.append({'suite':suite,'runtime':d['runtime'],'summary':d['summary'],'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    (output/'agentdojo-audit.json').write_text(json.dumps({'evidence_type':'historical; approval bypassed, not harness interception rate','suites':attacks},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k not in ('paired','source')},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
