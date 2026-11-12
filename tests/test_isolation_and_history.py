"""Identity scopes, explicit room access, authorized history and safe continuation."""
import asyncio
import hashlib
import json
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from api import state
from api.main import app
from api.deps import request_id
from core.scopes import identity_scope
from core.db import task_links
from core.events import InMemoryEventBus
from memory.conversation_memory import MemoryManager
from agents.task_recall import TaskRecall
from agents.task_runner import TaskAgentRunner
from agents.intent.taxonomy import IntentCategory, UrgencyLevel
from agents.types import OrchestratorResult
from tests.conftest import auth
from tests.test_shared_tasks import env as shared_env, proposal
from tests.test_task_execution import caps as execution_caps
from tests.test_chat_endpoint import chat_client as base_chat_client
from tests.test_workflow import ALICE, BOB, CAROL, ERIN, access
from workflow.store import ApprovalForbidden, ApprovalConflict
from workflow.tasks import TaskStore, TaskForbidden, TaskConflict

env, caps, chat_client = shared_env, execution_caps, base_chat_client


def create(tasks, owner=ALICE, name='GitLab 权限', key='one'):
    return tasks.create(owner, name, name + '需要跟进', key)


def invite(caps, task_id, principal=BOB, role='collaborator'):
    caps.access.observe(principal)
    caps.access.set_member(task_id, ALICE, principal.subject, role)


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
        m._profile.upsert(ids=[m._profile_doc_id(a)], documents=[json.dumps({'schema_version': 4, 'work': 'PROFILE-A'})], metadatas=[{'user_id': a}])
        own = await m.get_context(a, 'new', query='VPN')
        other = await m.get_context(b, 'old', query='VPN')
        assert own.relevant_history and 'PROFILE-A' in str(own.user_profile)
        assert 'LEGACY' not in str(own.relevant_history)
        assert not other.recent_messages and not other.relevant_history and not other.user_profile
        assert await m.get_turn_receipt(b, 'old', 'a') is None
        await m._redis.aclose()
    asyncio.run(run())


def test_private_chat_passes_scoped_identity_through_every_memory_port(chat_client, monkeypatch):
    from contextlib import asynccontextmanager
    from memory.conversation_memory import MemoryContext
    calls, receipts = [], {}

    class Memory:
        @asynccontextmanager
        async def conversation(self, user, _):
            calls.append(('lock', user))
            yield

        async def get_context(self, user, *_args, **_kwargs):
            calls.append(('read', user))
            return MemoryContext([], [], {}, '')

        async def get_turn_receipt(self, user, conv, rid):
            calls.append(('receipt', user))
            return receipts.get((user, conv, rid))

        async def commit_turn(self, user, conv, rid, message, response, *_args, **_kwargs):
            calls.append(('commit', user))
            receipts[user, conv, rid] = {'message_hash': hashlib.sha256(message.encode()).hexdigest(), 'response': response}

        async def store_episodic_turn(self, user, *_args):
            calls.append(('episodic', user))

    monkeypatch.setattr(state, 'memory', Memory())
    monkeypatch.setattr(state, 'profile_updater', SimpleNamespace(submit=lambda user, _: calls.append(('profile', user))))
    http, _ = chat_client
    payload = {'message': 'VPN 报错 VPN-691', 'conv_id': 'same', 'client_request_id': 'same'}
    own = http.post('/chat', json=payload)
    other = http.post('/chat', json=payload, headers=auth('alice', tenant='other'))
    assert own.status_code == other.status_code == 200
    assert http.post('/chat', json=payload).json() == own.json()
    assert own.json()['request_id'] != other.json()['request_id']
    for person in (ALICE, replace(ALICE, tenant='other')):
        assert {kind for kind, scope in calls if scope == person.scope_key} == {'lock', 'read', 'receipt', 'commit', 'episodic', 'profile'}
        assert calls.count(('commit', person.scope_key)) == 1
    assert all(scope != ALICE.subject for _, scope in calls)


def test_same_subject_other_tenant_cannot_resume_stream_or_read_trace(chat_client, monkeypatch):
    http, _ = chat_client
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


def test_legacy_approval_reads_votes_and_supersession_are_tenant_scoped(env, caps):
    _, workflow, _ = env
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


def test_new_room_denies_same_department_reviewer_until_explicit_invite(env, caps, monkeypatch):
    tasks, workflow, _ = env
    task = create(tasks)
    for person in (BOB, CAROL, replace(ALICE, tenant='other')):
        assert tasks.list(person) == []
        with pytest.raises(TaskForbidden):
            tasks.get(task['id'], person)
    tasks.finish(tasks.claim(), proposal())
    approval = tasks.get(task['id'], ALICE)['approvals'][0]
    assert not workflow.can_read(approval, BOB)
    assert workflow.list(BOB) == []
    invite(caps, task['id'])
    assert workflow.can_read(approval, BOB)
    caps.access.set_member(task['id'], ALICE, BOB.subject, 'revoked')
    assert not workflow.can_read(approval, BOB)
    monkeypatch.setattr(state, 'tasks', tasks)
    monkeypatch.setattr(state, 'task_capabilities', caps)
    monkeypatch.setattr(state, 'workflow', workflow)
    client = TestClient(app)
    assert client.get('/tasks/' + task['id'], headers=auth('bob')).status_code == 403
    assert client.get('/approvals/' + approval['approval_id'], headers=auth('bob')).status_code == 403


def test_bare_task_store_without_product_extensions_defaults_to_owner_and_tenant_admin(env):
    _, workflow, _ = env
    tasks = TaskStore(workflow.engine)
    task = create(tasks)
    assert tasks.access is None
    assert tasks.get(task['id'], ERIN)['id'] == task['id']
    for person in (BOB, replace(ALICE, tenant='other'), replace(ERIN, tenant='other')):
        with pytest.raises(TaskForbidden):
            tasks.get(task['id'], person)


def test_assignment_grants_only_selected_reviewer_access(env, caps):
    tasks, _, _ = env
    task = create(tasks)
    caps.access.observe(BOB)
    caps.access.assign(task['id'], ERIN, [BOB.subject])
    assert tasks.get(task['id'], BOB)['can_write']
    with pytest.raises(TaskForbidden):
        tasks.get(task['id'], CAROL)
    caps.access.assign(task['id'], ERIN, [])
    with pytest.raises(TaskForbidden):
        tasks.get(task['id'], BOB)


def test_assignment_removal_cannot_orphan_todos_or_revive_historical_participants(env, caps):
    tasks, _, _ = env
    task = create(tasks)
    caps.access.observe(BOB)
    caps.access.assign(task['id'], ERIN, [BOB.subject])
    tasks.post(task['id'], BOB, '审批人讨论', 'comment', False)
    payload = {'assignee': BOB.subject, 'question': '补充原因', 'due_at': None, 'client_request_id': 'todo'}
    todo = caps.collaboration.create(task['id'], ALICE, payload)
    with pytest.raises(TaskConflict, match='未完成待办'):
        caps.access.assign(task['id'], ERIN, [])
    caps.collaboration.change(task['id'], todo['id'], ALICE, 'reassign', ALICE.subject, '换人', 'move')
    caps.access.assign(task['id'], ERIN, [])
    with pytest.raises(TaskForbidden):
        caps.collaboration.create(task['id'], ALICE, {**payload, 'client_request_id': 'second'})
    with pytest.raises(TaskForbidden):
        caps.collaboration.change(task['id'], todo['id'], ALICE, 'reassign', BOB.subject, '回改', 'bad')


def test_cross_department_invite_and_observer_readonly(env, caps):
    tasks, _, _ = env
    task = create(tasks)
    person = replace(BOB, department='finance')
    invite(caps, task['id'], person, 'observer')
    assert tasks.get(task['id'], person)['can_write'] is False
    with pytest.raises(TaskForbidden):
        caps.history.continue_task(task['id'], person, 'continue', 'observer')
    caps.access.set_member(task['id'], ALICE, person.subject, 'collaborator')
    tasks.post(task['id'], person, '协作', 'cross-department', False)


def test_history_search_is_per_viewer_and_drops_revoked_sources(env, caps):
    tasks, _, _ = env
    own = create(tasks, name='GitLab secret', key='own')
    foreign = create(tasks, owner=replace(ALICE, tenant='other'), name='GitLab other tenant', key='other')
    other_user = create(tasks, owner=CAROL, name='GitLab other user', key='other-user')
    assert {x['id'] for x in caps.history.search(ALICE, '上次 GitLab')} == {own['id']}
    assert caps.history.search(BOB, 'GitLab') == []
    invite(caps, own['id'])
    assert {x['id'] for x in caps.history.search(BOB, 'GitLab')} == {own['id']}
    caps.access.set_member(own['id'], ALICE, BOB.subject, 'revoked')
    assert caps.history.search(BOB, 'GitLab') == []
    assert {x['id'] for x in caps.history.search(ERIN, 'GitLab')} == {own['id'], other_user['id']}
    assert foreign['id'] not in str(caps.history.search(ERIN, 'GitLab'))


def test_history_recall_and_snapshots_are_read_only(env, caps):
    from core.db import service_tasks
    tasks, _, _ = env
    own = create(tasks, name='GitLab secret', key='own')
    def version():
        with tasks.engine.connect() as db:
            return db.execute(select(service_tasks.c.version).where(service_tasks.c.id == own['id'])).scalar()
    before = version()
    for _ in range(3):
        assert [x['id'] for x in caps.history.recall_owned(ALICE, 'GitLab')] == [own['id']]
        caps.history.linked(own['id'], ALICE)
    assert version() == before  # private-chat recall must not bump versions or take write locks


def test_reference_does_not_copy_private_source_into_shared_room(env, caps):
    tasks, _, _ = env
    source = create(tasks, name='SECRET-SOURCE', key='source')
    destination = create(tasks, name='shared', key='destination')
    invite(caps, destination['id'])
    caps.history.link(destination['id'], source['id'], ALICE)
    assert [x['id'] for x in caps.history.linked(destination['id'], ALICE)] == [source['id']]
    assert caps.history.linked(destination['id'], BOB) == []
    shared = tasks.get(destination['id'], BOB)
    assert 'SECRET-SOURCE' not in str(shared) and source['id'] not in str(shared)
    invite(caps, source['id'])
    assert caps.history.linked(destination['id'], BOB)
    caps.access.set_member(source['id'], ALICE, BOB.subject, 'revoked')
    assert caps.history.linked(destination['id'], BOB) == []
    assert len(caps.history.linked(destination['id'], ALICE)) == 1


def test_links_require_source_access_owner_control_and_same_tenant(env, caps):
    tasks, _, _ = env
    source, destination = create(tasks, key='source'), create(tasks, key='destination')
    foreign = create(tasks, owner=replace(ALICE, tenant='other'), key='foreign')
    invite(caps, destination['id'])
    with pytest.raises(TaskForbidden):
        caps.history.link(destination['id'], source['id'], BOB)
    with pytest.raises(TaskForbidden):
        caps.history.link(destination['id'], foreign['id'], ALICE)
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: caps.history.link(destination['id'], source['id'], ALICE), range(2)))
    with tasks.engine.connect() as db:
        assert len(list(db.execute(select(task_links)))) == 1
    caps.history.link(destination['id'], source['id'], ALICE, False)
    assert not caps.history.linked(destination['id'], ALICE)


def test_history_snapshot_refreshes_workflow_not_stale_model_claim(env, caps):
    tasks, workflow, _ = env
    task = create(tasks)
    tasks.finish(tasks.claim(), proposal())
    draft = caps.history.snapshot(task['id'], ALICE)['approvals'][0]
    assert draft['status'] == 'draft'
    workflow.confirm(draft['approval_id'], ALICE)  # the history snapshot omits the hash; the store re-reads the frozen row
    before = caps.history.snapshot(task['id'], ALICE)
    approval = before['approvals'][0]
    assert approval['status'] == 'pending'
    invite(caps, task['id'])
    body = workflow.get(approval['approval_id'])
    workflow.decide(body['approval_id'], 'rejected', BOB, body['payload_hash'])
    assert caps.history.snapshot(task['id'], ALICE)['approvals'][0]['status'] == 'rejected'


def test_continuation_is_idempotent_readonly_and_uses_original_task(env, caps):
    tasks, _, _ = env
    task = create(tasks)
    tasks.finish(tasks.claim(), {'response': '等待'})
    tasks.stop(task['id'], ALICE)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: caps.history.continue_task(task['id'], ALICE, '继续', 'same'), range(2)))
    assert results[0] == results[1]
    assert tasks.get(task['id'], ALICE)['status'] == 'open'
    claim = tasks.claim()
    context, event, transcript = tasks.context(claim)
    assert event['kind'] == 'continuation'
    observed = []

    async def recognize(*args, **kwargs):
        return SimpleNamespace(intent=IntentCategory.PERMISSION_REQUEST, intent_group='access', confidence=1,
            urgency=UrgencyLevel.LOW, entities={}, missing_slots=[], recognition_source='test', requested_actions=frozenset({'request_access'}))

    async def run(req):
        observed.append(req)
        assert req.allowed_writes == frozenset() and 'request_access' in req.hidden_tools
        assert '续接时重新读取' in req.context and '"status": "open"' in req.context
        with pytest.raises(ValueError):
            await req.before_unsafe_tool('request_access')
        from agents.base_agent import BaseAgent
        return OrchestratorResult(req.request_id, '只读跟进', req.intent, observability=BaseAgent(None, 'test')._observability(req))

    runner = TaskAgentRunner(SimpleNamespace(recognize_intent=recognize, run=run, knowledge_version=lambda tenant: 0.), lambda _: {}, read_task_state=caps.history.runtime_context)
    tasks.finish(claim, asyncio.run(runner(context, event, transcript)))
    assert tasks.claim() is None and observed
    assert not tasks.get(task['id'], ALICE)['approvals']
    assert len(tasks.list(ALICE)) == 1
    with pytest.raises(TaskConflict):
        caps.history.continue_task(task['id'], ALICE, '不同消息', 'same')


def test_participant_cannot_reopen_stopped_source(env, caps):
    tasks, _, _ = env
    task = create(tasks)
    invite(caps, task['id'])
    tasks.stop(task['id'], ALICE)
    assert caps.history.snapshot(task['id'], BOB)['can_continue'] is False
    with pytest.raises(TaskForbidden):
        caps.history.continue_task(task['id'], BOB, 'reopen', 'bad')


def test_history_http_no_cross_tenant_and_reference_content_visibility(env, caps, monkeypatch):
    tasks, _, _ = env
    source, destination = create(tasks, key='source'), create(tasks, key='destination')
    monkeypatch.setattr(state, 'task_capabilities', caps)
    monkeypatch.setattr(state, 'task_worker', None)
    client = TestClient(app)
    assert client.get('/task-history', headers=auth('alice', tenant='other')).json()['items'] == []
    assert client.get('/task-history/' + source['id'], headers=auth('bob')).status_code == 403
    root = '/tasks/' + destination['id'] + '/links'
    assert client.post(root, headers=auth(), json={'target_id': source['id']}).status_code == 200
    invite(caps, destination['id'])
    assert client.get(root, headers=auth('bob')).json()['items'] == []
    result = client.post('/task-history/' + source['id'] + '/continue', headers=auth(), json={'message': '继续', 'client_request_id': 'http'})
    assert result.status_code == 200 and result.json()['task_id'] == source['id']


def test_private_recall_uses_own_tasks_and_current_tenant(env, caps):
    tasks, _, _ = env
    task = create(tasks, name='GitLab history')
    invite(caps, task['id'])
    recall = TaskRecall(caps.history.recall_owned)
    own = asyncio.run(recall.recall(ALICE, 'GitLab'))
    assert len(own) == 1 and own[0]['id'] == task['id']
    assert asyncio.run(recall.recall(BOB, 'GitLab')) == []  # invitations do not create durable personal copies
    assert asyncio.run(recall.recall(replace(ALICE, tenant='other'), 'GitLab')) == []
    assert '历史授权' in recall.prompt(own)


def test_private_chat_returns_live_history_references(env, caps, chat_client, monkeypatch):
    tasks, _, _ = env
    task = create(tasks, name='VPN history')
    monkeypatch.setattr(state, 'task_recall', TaskRecall(caps.history.recall_owned))
    http, _ = chat_client
    own = http.post('/chat', json={'message': 'VPN 报错 VPN-691', 'client_request_id': 'recall'}).json()
    other = http.post('/chat', json={'message': 'VPN 报错 VPN-691', 'client_request_id': 'recall'}, headers=auth('alice', tenant='other')).json()
    assert [x['id'] for x in own['task_references']] == [task['id']]
    assert other['task_references'] == []
