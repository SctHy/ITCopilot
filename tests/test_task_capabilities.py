"""Contract and fault tests for composed capabilities, runnable on SQLite or PostgreSQL."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from api import state
from api.main import app
from core.concurrency import LeaseLostError
from tests.conftest import auth
from tests.test_shared_tasks import env as shared_env, room, proposal

env = shared_env
from tests.test_workflow import ALICE, BOB, ERIN
from workflow.task_capabilities import TaskCapabilities
from workflow.tasks import TaskConflict, TaskForbidden
from workflow.task_worker import TaskWorker


@pytest.fixture
def caps(env):
    tasks, _, _ = env
    result = TaskCapabilities(tasks, tasks.effects.view)
    tasks.governance = result.governance
    return result


def progress(task, evidence, version=0):
    return dict(version=version, goal='权限生效并恢复访问', steps=[dict(title='验证访问', status='done', evidence_ids=[evidence])], blocker='', next_action='')


def test_acceptance_requires_evidence_authority_and_can_reopen(env, caps):
    tasks, _, _ = env
    task = room(tasks)
    with pytest.raises(TaskConflict):
        caps.progress.transition(task['id'], ALICE, 'resolved', '完成')
    data = progress(task, task['events'][0]['id'])
    with pytest.raises(TaskConflict):
        caps.progress.save(task['id'], ALICE, {**data, 'steps': [{**data['steps'][0], 'evidence_ids': ['foreign']}]})
    caps.progress.save(task['id'], ALICE, data)
    with pytest.raises(TaskConflict):
        caps.progress.save(task['id'], BOB, data)
    with pytest.raises(TaskForbidden):
        caps.progress.transition(task['id'], BOB, 'resolved', '我同意')
    active = tasks.claim()
    caps.progress.transition(task['id'], ALICE, 'resolved', '申请人确认可以访问')
    assert tasks.get(task['id'], ALICE)['phase'] == 'resolved'
    with pytest.raises(LeaseLostError):
        tasks.finish(active, {'response': '迟到结果'})
    caps.progress.transition(task['id'], ALICE, 'open', '')
    tasks.post(task['id'], ALICE, '还有访问问题', 'reopen')
    assert tasks.claim()


def test_pending_approval_and_question_block_acceptance(env, caps):
    tasks, _, _ = env
    task = room(tasks)
    tasks.finish(tasks.claim(), proposal())
    caps.progress.save(task['id'], ALICE, progress(task, task['events'][0]['id']))
    with pytest.raises(TaskConflict):
        caps.progress.transition(task['id'], ALICE, 'resolved', '完成')


def test_todo_assignee_deadline_reply_dedup_and_read_cursor(env, caps):
    tasks, _, clock = env
    task = room(tasks)
    payload = dict(assignee=ALICE.subject, question='请提供错误信息', due_at=clock.t + 10, client_request_id='ask')
    todo = caps.collaboration.create(task['id'], BOB, payload)
    assert caps.collaboration.create(task['id'], BOB, payload)['id'] == todo['id']
    with pytest.raises(TaskForbidden):
        caps.collaboration.create(task['id'], BOB, {**payload, 'assignee': 'other'})
    with pytest.raises(TaskForbidden):
        caps.collaboration.answer(task['id'], todo['id'], BOB, '不可以代答')
    clock.t += 20
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: caps.deadlines.poll(), range(2)))
    assert sum(e['kind'] == 'reminder' for e in tasks.get(task['id'], ALICE)['events']) == 1
    assert caps.inbox(ALICE)['items'][0]['pending_answers'] == 1
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: caps.collaboration.answer(task['id'], todo['id'], ALICE, '403'), range(2)))
    body = tasks.get(task['id'], ALICE)
    assert sum(e['kind'] == 'todo_answer' for e in body['events']) == 1
    assert caps.inbox(ALICE)['items'][0]['pending_answers'] == 0
    caps.collaboration.mark_read(task['id'], ALICE, body['events'][-1]['seq'])
    assert caps.inbox(ALICE)['items'][0]['unread'] == 0
    with pytest.raises(TaskConflict):
        caps.collaboration.mark_read(task['id'], ALICE, 999999)


def test_persisted_timers_fire_once_and_closed_tasks_cancel(env, caps):
    tasks, _, clock = env
    task = room(tasks)
    tasks.finish(tasks.claim(), {'response': '等待'})
    timer = caps.reminders.create(task['id'], ALICE, dict(text='复查访问', due_at=clock.t + 10, wake=True, client_request_id='timer'))
    restored = TaskCapabilities(tasks, tasks.effects.view)
    clock.t += 11
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: restored.reminders.poll(), range(2)))
    claim = tasks.claim()
    assert tasks.context(claim)[1]['data']['timer_id'] == timer['id']
    tasks.finish(claim, {'response': '已复查'})
    assert tasks.claim() is None
    second = caps.reminders.create(task['id'], ALICE, dict(text='后续', due_at=clock.t + 10, wake=True, client_request_id='next'))
    tasks.stop(task['id'], ALICE)
    caps.progress.transition(task['id'], ALICE, 'open', '')
    clock.t += 11
    caps.reminders.poll()
    assert tasks.claim() is None
    assert next(t for t in caps.reminders.list(task['id'], ALICE) if t['id'] == second['id'])['status'] == 'cancelled'


def test_budget_holds_next_run_tracks_attempts_and_admin_resumes(env, caps):
    tasks, _, _ = env
    task = room(tasks)
    caps.governance.configure(task['id'], ERIN, 10, 1)
    claim = tasks.claim()
    tasks.finish(claim, {'response': '完成', 'token_usage': {'input_tokens': 8, 'output_tokens': 4},
                         'tool_traces': [{'tool_name': 'check', 'success': True, 'output': 'secret'}]})
    tasks.post(task['id'], ALICE, '继续', 'next')
    assert tasks.claim() is None
    assert tasks.get(task['id'], ALICE)['phase'] == 'budget_paused'
    ledger = caps.governance.read(task['id'], ALICE)
    assert ledger['budget']['used_tokens'] == 12
    assert 'secret' not in str(ledger)
    with pytest.raises(TaskForbidden):
        caps.governance.configure(task['id'], BOB, 100, 10)
    caps.governance.configure(task['id'], ERIN, 100, 10)
    assert tasks.claim()


def test_expired_attempt_keeps_unknown_usage_and_retry_failure(env, caps):
    tasks, _, clock = env
    task = room(tasks)
    tasks.claim(10)
    clock.t += 11
    second = tasks.claim(10)
    tasks.fail(second, 'timeout')
    rows = caps.governance.read(task['id'], ALICE)['attempts']
    assert {a['status'] for a in rows} == {'interrupted', 'failed'}
    assert caps.governance.read(task['id'], ALICE)['budget']['used_runs'] == 2


def test_extensions_http_tenant_and_payload_boundaries(env, caps, monkeypatch):
    tasks, _, _ = env
    task = room(tasks)
    monkeypatch.setattr(state, 'task_capabilities', caps)
    client = TestClient(app)
    root = '/tasks/' + task['id']
    assert client.get(root + '/workspace', headers=auth('bob')).status_code == 200
    assert client.get(root + '/workspace', headers=auth('bob', tenant='other')).status_code == 403
    assert client.post(root + '/budget', json={'token_limit': 100, 'run_limit': 5}, headers=auth('bob')).status_code == 403
    assert client.post(root + '/reminders', json={'text': 'x', 'due_at': 'NaN', 'client_request_id': 'x'}, headers=auth()).status_code == 422
    assert client.post(root + '/progress', json=progress(task, task['events'][0]['id']), headers=auth()).status_code == 200
    assert client.get('/task-inbox', headers=auth('bob', tenant='other')).json()['items'] == []
    for method in (caps.progress.read, caps.collaboration.list, caps.reminders.list, caps.governance.read):
        with pytest.raises(TaskForbidden):
            method(task['id'], replace(BOB, tenant='other'))


def test_answer_and_reminder_followups_are_readonly_and_survive_worker_restart(env, caps):
    tasks, _, clock = env
    task = room(tasks)
    tasks.finish(tasks.claim(), {'response': '等待材料'})
    todo = caps.collaboration.create(task['id'], BOB, dict(assignee=ALICE.subject, question='错误码', due_at=None, client_request_id='q'))
    caps.collaboration.answer(task['id'], todo['id'], ALICE, '403')
    observed = []

    async def handler(task, event, history):
        observed.append(event['kind'])
        return {'response': '收到材料，继续只读检查'}

    asyncio.run(TaskWorker(tasks, handler).run_once())
    assert observed == ['todo_answer']
    assert not tasks.claim()


def test_budget_exhaustion_after_crash_marks_previous_attempt_interrupted(env, caps):
    tasks, _, clock = env
    task = room(tasks)
    caps.governance.configure(task['id'], ERIN, 100, 1)
    tasks.claim(10)
    clock.t += 11
    assert tasks.claim() is None
    ledger = caps.governance.read(task['id'], ALICE)
    assert ledger['attempts'][0]['status'] == 'interrupted'
    assert tasks.get(task['id'], ALICE)['phase'] == 'budget_paused'


def test_progress_evidence_survives_recent_history_window(env, caps):
    tasks, _, _ = env
    task = room(tasks)
    evidence = task['events'][0]['id']
    caps.progress.save(task['id'], ALICE, progress(task, evidence))
    for i in range(35):
        tasks.post(task['id'], BOB, f'讨论 {i}', str(i), False)
    context = caps.progress.context(task['id'])
    assert context['evidence'][0]['text'] == task['events'][0]['text']
    assert 'principal' not in str(context)


def test_approval_deadline_reminds_once_and_never_votes(env, caps):
    from workflow.task_approvals import ApprovalDeadlineSource
    from core.db import approvals
    from sqlalchemy import select
    tasks, workflow, clock = env
    task = room(tasks)
    tasks.finish(tasks.claim(), proposal())
    with tasks.engine.connect() as db:
        expires = db.execute(select(approvals.c.expires_at)).scalar()
    clock.t = expires - 100
    source = ApprovalDeadlineSource(tasks)
    source.poll(); source.poll()
    body = tasks.get(task['id'], ALICE)
    assert sum(e['kind'] == 'approval_reminder' for e in body['events']) == 1
    assert body['approvals'][0]['status'] == 'pending'
    assert tasks.claim() is None


def test_reopening_clears_acceptance_and_each_new_stop_has_own_event(env, caps):
    tasks, _, _ = env
    task = room(tasks)
    tasks.stop(task['id'], ALICE)
    tasks.stop(task['id'], ALICE)
    caps.progress.transition(task['id'], ALICE, 'open', '')
    tasks.stop(task['id'], ALICE)
    body = tasks.get(task['id'], ALICE)
    assert sum(e['kind'] == 'system' and '已停止' in e['text'] for e in body['events']) == 2
    caps.progress.transition(task['id'], ALICE, 'open', '')
    caps.progress.save(task['id'], ALICE, progress(task, task['events'][0]['id']))
    caps.progress.transition(task['id'], ALICE, 'resolved', '确认已完成')
    tasks.stop(task['id'], ALICE)
    assert tasks.get(task['id'], ALICE)['status'] == 'resolved'
    caps.progress.transition(task['id'], ALICE, 'open', '')
    assert caps.progress.read(task['id'], ALICE)['resolution'] == ''
