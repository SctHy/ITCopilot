"""Product capability contracts, independent transports, and cross-entry authorization."""
import asyncio
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from api import state
from api.main import app
from core.identity import Principal
from tests.conftest import auth
from tests.test_shared_tasks import env as shared_env, room, proposal
from tests.test_workflow import ALICE, BOB, CAROL, ERIN
from workflow.task_capabilities import TaskCapabilities
from workflow.task_ports import CompositeTaskEffects
from workflow.task_notifications import TaskNotifications, TaskNotificationWorker
from workflow.tasks import TaskConflict, TaskForbidden
from workflow.store import ApprovalForbidden

env = shared_env
DAVE = Principal('dev:dave', 'dave', 'engineering', frozenset({'employee'}), '1002')


@pytest.fixture
def product(env):
    tasks, workflow, _ = env
    caps = TaskCapabilities(tasks, tasks.effects.view)
    tasks.effects = CompositeTaskEffects([tasks.effects, caps.suggestions])
    workflow.shared_access, workflow.shared_filter = caps.access.approval_access, caps.access.approval_filter
    for p in (ALICE, BOB, CAROL, ERIN, DAVE):
        caps.access.observe(p)
    return caps


def test_todo_reassign_and_cancel_unblock_acceptance_with_audit(env, product):
    tasks, _, _ = env
    task = room(tasks)
    todo = product.collaboration.create(task['id'], BOB, dict(assignee=ALICE.subject, question='错误码', due_at=None, client_request_id='q'))
    product.collaboration.change(task['id'], todo['id'], BOB, 'reassign', BOB.subject, '我先核查', 'move')
    with pytest.raises(TaskForbidden):
        product.collaboration.answer(task['id'], todo['id'], ALICE, '过时的答复')
    product.collaboration.change(task['id'], todo['id'], BOB, 'cancel', None, '问题已无效', 'cancel')
    product.collaboration.change(task['id'], todo['id'], BOB, 'cancel', None, '问题已无效', 'cancel')
    assert product.collaboration.list(task['id'], ALICE)[0]['status'] == 'cancelled'
    assert len([e for e in tasks.get(task['id'], ALICE)['events'] if e['kind'] == 'todo_changed']) == 2


def test_member_observer_revocation_and_legacy_approvals_share_acl(env, product):
    tasks, workflow, _ = env
    task = room(tasks)
    tasks.finish(tasks.claim(), proposal())
    approval = tasks.get(task['id'], ALICE)['approvals'][0]
    product.access.set_member(task['id'], ALICE, DAVE.subject, 'observer')
    assert tasks.get(task['id'], DAVE)['can_write'] is False
    assert workflow.can_read(approval, DAVE)
    assert len(workflow.list(DAVE)) == 1
    with pytest.raises(TaskForbidden):
        tasks.post(task['id'], DAVE, '不能写', 'no')
    product.collaboration.mark_read(task['id'], DAVE, 1)
    product.access.set_member(task['id'], ALICE, DAVE.subject, 'collaborator')
    tasks.post(task['id'], DAVE, '可以讨论', 'yes', False)
    product.access.set_member(task['id'], ALICE, DAVE.subject, 'revoked')
    assert not workflow.can_read(approval, DAVE)
    assert not workflow.list(DAVE)
    with pytest.raises(TaskForbidden):
        tasks.get(task['id'], DAVE)
    product.access.set_member(task['id'], ALICE, BOB.subject, 'revoked')
    with pytest.raises(ApprovalForbidden):
        workflow.decide(approval['approval_id'], 'approved', BOB, approval['payload_hash'])
    assert not product.query.page(BOB)['items']
    with pytest.raises(TaskForbidden):
        product.access.set_member(task['id'], ALICE, 'forged', 'collaborator')


def test_approval_assignment_applies_to_direct_workflow_and_both_http_entries(env, product, monkeypatch):
    tasks, workflow, _ = env
    task = room(tasks)
    tasks.finish(tasks.claim(), proposal())
    approval = tasks.get(task['id'], ALICE)['approvals'][0]
    with pytest.raises(TaskForbidden):
        product.access.assign(task['id'], ALICE, [BOB.subject])
    product.access.assign(task['id'], ERIN, [CAROL.subject])
    monkeypatch.setattr(state, 'workflow', workflow); monkeypatch.setattr(state, 'tasks', tasks)
    monkeypatch.setattr(state, 'task_capabilities', product); monkeypatch.setattr(state, 'worker', None)
    client = TestClient(app)
    vote = {'decision': 'approved', 'payload_hash': approval['payload_hash']}
    for path in ('/approvals/' + approval['approval_id'], '/tasks/' + task['id'] + '/approvals/' + approval['approval_id']):
        assert client.post(path, headers=auth('bob'), json=vote).status_code == 403
    assert client.post('/approvals/' + approval['approval_id'], headers=auth('carol'), json=vote).status_code == 200


def test_search_pagination_personal_archive_and_event_pages(env, product):
    tasks, _, _ = env
    for i in range(103):
        tasks.create(ALICE, 'historical ' + str(i), '搜索证据 needle' if i == 0 else 'plain', str(i))
    first = product.query.page(ALICE, limit=25)
    second = product.query.page(ALICE, offset=25, limit=25)
    assert first['total'] == 103 and first['has_more']
    assert not {t['id'] for t in first['items']} & {t['id'] for t in second['items']}
    found = product.query.page(ALICE, q='needle')['items'][0]
    product.query.archive(found['id'], ALICE, True)
    assert product.query.page(ALICE, q='needle')['total'] == 0
    assert product.query.page(ALICE, q='needle', archived=True)['total'] == 1
    assert product.query.page(BOB, q='needle')['total'] == 0  # not invited into the separately created room
    for i in range(6):
        tasks.post(found['id'], ALICE, str(i), str(i), False)
    latest = product.query.events(found['id'], ALICE, limit=3)
    older = product.query.events(found['id'], ALICE, before=latest['items'][0]['seq'], limit=3)
    assert latest['has_more'] and not {e['id'] for e in latest['items']} & {e['id'] for e in older['items']}
    assert product.query.page(replace(BOB, tenant='other'))['total'] == 0


def test_attachments_are_idempotent_evidence_and_revocation_protected(env, product, monkeypatch):
    tasks, _, _ = env
    task = room(tasks)
    file = product.files.upload(task['id'], ALICE, '../../错误.log', b'error 403', 'upload')
    assert file['name'] == '错误.log' and file['size'] == 9
    assert product.files.upload(task['id'], ALICE, '../../错误.log', b'error 403', 'upload')['id'] == file['id']
    with pytest.raises(TaskConflict):
        product.files.upload(task['id'], ALICE, 'x.log', b'different', 'upload')
    product.access.set_member(task['id'], ALICE, DAVE.subject, 'observer')
    assert product.files.download(task['id'], file['id'], DAVE)['content'] == b'error 403'
    product.access.set_member(task['id'], ALICE, DAVE.subject, 'revoked')
    with pytest.raises(TaskForbidden):
        product.files.download(task['id'], file['id'], DAVE)
    monkeypatch.setattr(state, 'task_capabilities', product)
    client = TestClient(app)
    response = client.get('/tasks/' + task['id'] + '/files/' + file['id'], headers=auth())
    assert response.headers['content-type'] == 'application/octet-stream'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.content == b'error 403'


def test_suggestions_are_staged_and_cannot_overwrite_newer_progress(env, product):
    tasks, _, _ = env
    task = room(tasks)
    payload = dict(goal='恢复访问', steps=[dict(title='检查访问', status='working', evidence_ids=[])], blocker='', next_action='排查')
    tasks.finish(tasks.claim(), {'response': '建议排查', 'progress_suggestion': payload, 'progress_version': 0})
    suggestion = product.suggestions.list(task['id'], ALICE)[0]
    assert product.progress.read(task['id'], ALICE)['version'] == 0
    product.progress.save(task['id'], ALICE, {**payload, 'version': 0})
    with pytest.raises(TaskConflict):
        product.suggestions.decide(task['id'], suggestion['id'], BOB, True)
    assert product.suggestions.list(task['id'], ALICE)[0]['status'] == 'pending'
    product.suggestions.decide(task['id'], suggestion['id'], BOB, False)


def test_suggestion_adoption_commits_with_state_and_is_idempotent(env, product):
    tasks, _, _ = env
    task = room(tasks)
    payload = dict(goal='恢复访问', steps=[dict(title='检查', status='todo', evidence_ids=[])], blocker='', next_action='排查')
    tasks.finish(tasks.claim(), {'response': '建议', 'progress_suggestion': payload, 'progress_version': 0})
    item = product.suggestions.list(task['id'], ALICE)[0]
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: product.suggestions.decide(task['id'], item['id'], ALICE, True), range(2)))
    assert product.progress.read(task['id'], ALICE)['version'] == 1
    assert tasks.get(task['id'], ALICE)['status'] == 'open'


def test_notifications_retry_restart_escalation_and_unsubscribe(env, product):
    tasks, _, clock = env
    task = room(tasks)
    sent = []

    class Transport:
        async def send(self, *args):
            if not sent:
                sent.append('failure')
                raise OSError('temporary')
            sent.append(args)

    service = TaskNotifications(tasks, {'default:' + ALICE.subject: {'email': 'alice@test.invalid'},
        'default:' + BOB.subject: {'email': 'bob@test.invalid'}}, {'email': Transport()})
    service.subscribe(task['id'], ALICE, 'email', True, BOB.subject)
    tasks.ingest(task['id'], source='timer', object_id='one', version=1, kind='reminder', text='已到期', wake=False)
    service.collect(); service.collect()
    worker = TaskNotificationWorker(service)
    asyncio.run(worker.run_once())
    clock.t += 10
    asyncio.run(worker.run_once()); asyncio.run(worker.run_once())
    assert len([s for s in sent if isinstance(s, tuple)]) == 2
    assert {s[0] for s in sent if isinstance(s, tuple)} == {'alice@test.invalid', 'bob@test.invalid'}
    tasks.post(task['id'], BOB, '新消息', 'notify', False)
    service.collect()
    service.subscribe(task['id'], ALICE, 'email', False)
    asyncio.run(worker.run_once())
    assert len(sent) == 3


def test_notifications_recheck_membership_before_send(env, product):
    tasks, _, _ = env
    task = room(tasks)
    product.access.set_member(task['id'], ALICE, DAVE.subject, 'collaborator')
    service = TaskNotifications(tasks, {'default:' + DAVE.subject: {'email': 'dave@test.invalid'}}, {'email': SimpleNamespace()})
    service.subscribe(task['id'], DAVE, 'email', True)
    tasks.post(task['id'], ALICE, 'notice', 'n', False)
    service.collect()
    product.access.set_member(task['id'], ALICE, DAVE.subject, 'revoked')
    claim = service.claim()
    assert service.message(claim) is None


def test_notification_expired_leases_cannot_settle_and_crashes_exhaust_retry_limit(env, product):
    from sqlalchemy import select
    from core.db import task_deliveries
    tasks, _, clock = env
    task = room(tasks)
    service = TaskNotifications(tasks, {'default:' + ALICE.subject: {'email': 'alice@test.invalid'}}, {'email': SimpleNamespace()})
    service.subscribe(task['id'], ALICE, 'email', True)
    tasks.post(task['id'], BOB, 'notice', 'n', False)
    service.collect()
    for attempt in range(1, 6):
        claim = service.claim()
        assert claim['attempts'] == attempt
        clock.t += 61
        service.settle(claim, 'sent')
        with tasks.engine.connect() as db:
            assert db.execute(select(task_deliveries.c.status)).scalar_one() == 'sending'
    assert service.claim() is None
    with tasks.engine.connect() as db:
        assert db.execute(select(task_deliveries.c.status)).scalar_one() == 'failed'


def test_invalid_progress_advice_keeps_known_usage_and_unproven_steps_are_not_done():
    import agents.task_progress_advisor as module
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage
    response = ModelResponse(parts=[TextPart('not JSON')], usage=RequestUsage(input_tokens=11, output_tokens=7))

    def request(*args, **kwargs):
        return response

    advisor = module.TaskProgressAdvisor(FunctionModel(request))
    args = ({}, {'title': '访问'}, {'text': '排查'}, '回复', 'reply-id')
    payload, usage = asyncio.run(advisor.suggest(*args))
    assert payload is None and usage == {'input_tokens': 22, 'output_tokens': 14}
    response.parts = [ToolCallPart('draft_progress', {'goal': '恢复访问', 'steps': [{'title': '排查', 'status': 'done', 'evidence_ids': ['forged']}]})]
    payload, usage = asyncio.run(advisor.suggest(*args))
    assert payload['steps'][0]['status'] == 'working'
    assert payload['steps'][0]['evidence_ids'] == []


def test_identity_observe_is_cached_and_never_fails_authentication(env, product, monkeypatch):
    from api.deps import current_principal
    from core.identity import issue_dev_token
    product.access._observed.clear()
    queries = []
    real = product.access.tasks.engine.connect
    monkeypatch.setattr(product.access.tasks.engine, 'connect', lambda *a, **k: (queries.append(1), real(*a, **k))[1])
    for _ in range(3):
        product.access.observe(ALICE)
    assert len(queries) == 1  # unchanged profile skips the DB on later requests

    def broken(_):
        raise RuntimeError('database is locked')
    monkeypatch.setattr(state, 'task_capabilities', SimpleNamespace(access=SimpleNamespace(observe=broken)))
    assert current_principal('Bearer ' + issue_dev_token('alice')).subject  # /chat etc. must not 500
