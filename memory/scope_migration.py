"""Explicit, one-owner migration of legacy long-term memories; no unscoped fallback."""
import hashlib
import json
import time
from sqlalchemy import select, insert, update
from core.db import vector_documents, memory_scope_bindings
from core.scopes import identity_scope


def migrate_legacy(engine, bindings, *, apply=False):
    owners = {}
    for entry in bindings:
        subject, tenant = entry['subject'], entry['tenant']
        if not isinstance(subject, str) or not isinstance(tenant, str) or not subject or not tenant or subject.startswith('identity:v1:'):
            raise ValueError('bindings require nonempty legacy subject and tenant')
        if subject in owners and owners[subject] != tenant:
            raise ValueError('one legacy subject cannot be assigned to multiple tenants')
        owners[subject] = tenant
    stats = {'mode': 'apply' if apply else 'preview', 'copied': 0, 'already_present': 0, 'profile_conflicts': 0, 'bindings': len(owners)}
    with engine.begin() as db:
        for subject, tenant in sorted(owners.items()):
            scope = identity_scope(tenant, subject)
            old = db.execute(select(memory_scope_bindings).where(memory_scope_bindings.c.legacy_subject == subject)).mappings().first()
            if old and old['scope_key'] != scope:
                raise ValueError('legacy subject is already assigned to a different tenant')
            if apply:
                if old:
                    # Serialize concurrent same-binding copies, then re-read target rows.
                    db.execute(update(memory_scope_bindings).where(memory_scope_bindings.c.legacy_subject == subject).values(scope_key=scope))
                else:
                    db.execute(insert(memory_scope_bindings).values(legacy_subject=subject, tenant=tenant, scope_key=scope, migrated_at=time.time()))
            rows = db.execute(select(vector_documents).where(vector_documents.c.collection.in_(['episodic', 'user_profile']),
                vector_documents.c.meta['user_id'].as_string() == subject)
                .order_by(vector_documents.c.updated_at.desc(), vector_documents.c.id)).mappings().all()
            planned = {}
            for row in rows:
                meta = row['meta'] or {}
                if meta.get('tenant') and meta['tenant'] != tenant:
                    raise ValueError('stored tenant disagrees with the supplied binding')
                target = ('user_profile:' + scope if row['collection'] == 'user_profile' else
                          'legacy:' + hashlib.sha256(json.dumps([row['collection'], row['id'], scope]).encode()).hexdigest())
                target_key = (row['collection'], target)
                current = planned.get(target_key)
                if current is None:
                    current = db.execute(select(vector_documents.c.meta).where(vector_documents.c.collection == row['collection'], vector_documents.c.id == target)).scalar()
                if current is not None:
                    stats['already_present' if current.get('legacy_source') == row['id'] else 'profile_conflicts'] += 1
                    continue
                stats['copied'] += 1
                planned[target_key] = {**meta, 'user_id': scope, 'tenant': tenant, 'legacy_source': row['id']}
                if apply:
                    db.execute(insert(vector_documents).values(**{**dict(row), 'id': target,
                        'meta': planned[target_key]}))
    return stats
