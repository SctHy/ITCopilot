"""Identity scopes, explicit room access, authorized history and safe continuation."""
import asyncio
import hashlib
from dataclasses import replace

import fakeredis.aioredis
import pytest

from tests.runtime import state
from servicedesk_agent.api.deps import request_id
from servicedesk_agent.infra.scopes import identity_scope
from servicedesk_agent.infra.events import InMemoryEventBus
from servicedesk_agent.agent.memory.conversation_memory import MemoryManager
from tests.conftest import auth
from tests.test_chat_endpoint import chat_client as base_chat_client
from tests.test_workflow import ALICE, BOB, ERIN, access
from servicedesk_agent.workflow.store import ApprovalForbidden, ApprovalConflict
chat_client = base_chat_client
from tests.test_workflow import env  # noqa: F401


def test_scope_separates_same_subject_tenants_and_delimiter_collisions():
    assert ALICE.scope_key != replace(ALICE, tenant='other').scope_key
    assert identity_scope('a:b', 'c') != identity_scope('a', 'b:c')
    assert request_id(ALICE, 'same') != request_id(replace(ALICE, tenant='other'), 'same')
    assert request_id(replace(ALICE, tenant='a:b', subject='c'), 'x') != request_id(replace(ALICE, tenant='a', subject='b:c'), 'x')


def test_private_working_and_cross_conversation_memory_are_scoped(db_url):
    async def run():
        m = MemoryManager(database_url=db_url, api_key='sk-local')
        m._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        a, b = ALICE.scope_key, replace(ALICE, tenant='other').scope_key
        await m.commit_turn(a, 'old', 'a', 'VPN SECRET-A', {'request_id': 'a', 'response': '证书过期'})
        await m.store_episodic_turn(a, 'old', 'a', 'VPN SECRET-A', '证书过期')
        await m.store_episodic_turn(ALICE.subject, 'legacy', 'legacy', 'VPN LEGACY', '旧记录')
        own = await m.get_context(a, 'new', query='VPN')
        other = await m.get_context(b, 'old', query='VPN')
        assert own.relevant_history
        assert 'LEGACY' not in str(own.relevant_history)
        assert not other.recent_messages and not other.relevant_history
        assert await m.get_turn_receipt(b, 'old', 'a') is None
        await m._redis.aclose()
    asyncio.run(run())


def test_private_chat_passes_scoped_identity_through_every_memory_port(chat_client, monkeypatch):
    from contextlib import asynccontextmanager
    from servicedesk_agent.agent.memory.conversation_memory import MemoryContext
    calls, receipts = [], {}

    class Memory:
        @asynccontextmanager
        async def conversation(self, user, _):
            calls.append(('lock', user))
            yield

        async def get_context(self, user, *_args, **_kwargs):
            calls.append(('read', user))
            return MemoryContext([], [], '')

        async def get_turn_receipt(self, user, conv, rid):
            calls.append(('receipt', user))
            return receipts.get((user, conv, rid))

        async def commit_turn(self, user, conv, rid, message, response, *_args, **_kwargs):
            calls.append(('commit', user))
            receipts[user, conv, rid] = {'message_hash': hashlib.sha256(message.encode()).hexdigest(), 'response': response}

        async def store_episodic_turn(self, user, *_args):
            calls.append(('episodic', user))

    monkeypatch.setattr(state, 'memory', Memory())
    http = chat_client
    payload = {'message': 'VPN 报错 VPN-691', 'conv_id': 'same', 'client_request_id': 'same'}
    own = http.post('/chat', json=payload)
    other = http.post('/chat', json=payload, headers=auth('alice', tenant='other'))
    assert own.status_code == other.status_code == 200
    assert http.post('/chat', json=payload).json() == own.json()
    assert own.json()['request_id'] != other.json()['request_id']
    for person in (ALICE, replace(ALICE, tenant='other')):
        assert {kind for kind, scope in calls if scope == person.scope_key} == {'lock', 'read', 'receipt', 'commit', 'episodic'}
        assert calls.count(('commit', person.scope_key)) == 1
    assert all(scope != ALICE.subject for _, scope in calls)


def test_same_subject_other_tenant_cannot_resume_stream_or_read_trace(chat_client, monkeypatch):
    http = chat_client
    monkeypatch.setattr(state, 'events', InMemoryEventBus())
    first = http.post('/chat/stream', json={'message': 'VPN 报错 VPN-691', 'conv_id': 'same', 'client_request_id': 'tenant-key'})
    assert 'event: completed' in first.text
    rid = request_id(ALICE, 'tenant-key')
    assert http.get('/chat/stream/' + rid, headers=auth('alice', tenant='other')).status_code == 403

    class Traces:
        async def get_trace(self, _):
            return {'observability': {'owner': ALICE.subject, 'tenant': ALICE.tenant}, 'response': 'private'}
        async def recent_traces(self, _):
            return [await self.get_trace('x'), {'observability': {'owner': ALICE.subject}, 'response': 'unscoped'}]

    monkeypatch.setattr(state, 'memory', Traces())
    for user in ('alice', 'erin'):
        assert http.get('/trace/' + rid, headers=auth(user, tenant='other')).status_code == 403
    assert http.get('/trace/' + rid, headers=auth()).status_code == 200
    assert http.get('/trace/tools', headers=auth('erin', tenant='other')).json()['items'] == []
    assert len(http.get('/trace/tools', headers=auth('erin')).json()['items']) == 1


def test_approval_reads_votes_and_supersession_are_tenant_scoped(env):  # noqa: F811
    workflow, _, _ = env
    first = workflow.create('old', request_id='r1', conv_id='same', principal=ALICE, action_requests=[access()])
    foreign = replace(ALICE, tenant='other')
    workflow.create('foreign', request_id='r2', conv_id='same', principal=foreign, action_requests=[access()])
    assert workflow.get('old')['status'] == 'pending'
    assert [r['approval_id'] for r in workflow.list(replace(BOB, tenant='other'))] == ['foreign']
    for caller in (foreign, replace(BOB, tenant='other'), replace(ERIN, tenant='other')):
        assert not workflow.can_read(first, caller)
    with pytest.raises(ApprovalForbidden):
        workflow.decide('old', 'approved', replace(BOB, tenant='other'), first['payload_hash'])
    with pytest.raises(ApprovalConflict):
        workflow.create('old', request_id='r1', conv_id='same', principal=foreign, action_requests=[access()])
    workflow.decide('old', 'approved', BOB, first['payload_hash'])
    with pytest.raises(ApprovalForbidden):
        workflow.decide('old', 'approved', replace(BOB, tenant='other'), first['payload_hash'])
