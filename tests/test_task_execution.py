"""Checkpoint crash recovery and independent acceptance contracts; SQLite and PostgreSQL."""
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from agents.agent_orchestrator import AgentOrchestrator
from agents.task_runner import TaskAgentRunner
from agents.types import AgentResponse
from agents.intent.taxonomy import IntentCategory, UrgencyLevel
from api import state
from api.main import app
from core.concurrency import LeaseLostError
from core.db import task_checkpoints, task_verifications, task_runs
from tests.conftest import auth, run_in_tenant
from tests.test_shared_tasks import env as shared_env, room, proposal
from tests.test_task_capabilities import progress
from tests.test_workflow import ALICE, BOB
from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from workflow.goal_verifiers import AccessGoalVerifier
from workflow.task_capabilities import TaskCapabilities
from workflow.task_ports import CompositeTaskEffects
from workflow.task_verification import TaskVerificationWorker
from workflow.tasks import TaskConflict, TaskForbidden
from workflow.task_worker import TaskWorker

env = shared_env
CHECKS = [{'kind': 'access_level', 'system': 'gitlab', 'role': 'readonly'}]


@pytest.fixture
def caps(env):
    tasks, workflow, _ = env
    result = TaskCapabilities(tasks, tasks.effects.view, AccessGoalVerifier(AdapterRegistry(LocalDirectoryAdapter(workflow.engine))))
    tasks.governance = result.governance
    tasks.effects = CompositeTaskEffects([tasks.effects, result.goals])
    return result


def fake_orchestrator(calls):
    """Real orchestrator run path and checkpointing, deterministic intent and agent; no external model call."""
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    from agents.base_agent import BaseAgent
    orchestrator.agent = BaseAgent(None, "test")
    orchestrator.knowledge_version = lambda tenant: 0.0

    async def recognize(*args, **kwargs):
        calls['intent'] += 1
        return SimpleNamespace(intent=IntentCategory.PERMISSION_REQUEST, intent_group='access', confidence=1,
            urgency=UrgencyLevel.LOW, entities={}, missing_slots=[], recognition_source="test", requested_actions=frozenset({'request_access'}))

    async def execute(req):
        calls['agent'] += 1
        req.route_metadata = {"model": "deepseek-flash", "route": "agent_write", "route_reason": "primary", "cost_usd": 0.0}
        return AgentResponse('结果', True, token_usage={'input_tokens': 7, 'output_tokens': 3}, observability=orchestrator.agent._observability(req))

    orchestrator.recognize_intent, orchestrator._execute = recognize, execute
    return orchestrator


class CrashingAdvisor:
    """Simulates the process disappearing after the agent step's durable commit."""

    async def suggest(self, *args):
        raise asyncio.CancelledError()


async def run_claim(tasks, caps, claim, orchestrator, advisor=None):
    task, event, transcript = tasks.context(claim)
    task['_claim'] = claim
    return await TaskAgentRunner(orchestrator, lambda _: {}, caps.progress.context, advisor=advisor, plan_store=caps.plan_store)(task, event, transcript)


def ready_task(tasks, caps, *, limit=0):
    task = room(tasks)
    tasks.finish(tasks.claim(), {'response': '已完成'})
    caps.progress.save(task['id'], ALICE, progress(task, task['events'][0]['id']))
    caps.goals.configure(task['id'], ALICE, 0, CHECKS, limit)
    return task


def test_restart_reuses_completed_agent_step_frozen_input_and_known_cost(env, caps):
    tasks, _, clock = env
    task = room(tasks)
    first, calls = tasks.claim(5), Counter()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_claim(tasks, caps, first, fake_orchestrator(calls), advisor=CrashingAdvisor()))
    assert caps.governance.read(task['id'], ALICE)['budget']['used_tokens'] == 10
    # Human progress changing during downtime does not silently change an already-running turn's input.
    caps.progress.save(task['id'], ALICE, progress(task, task['events'][0]['id']))
    clock.t += 6
    current = tasks.claim(5)
    result = asyncio.run(run_claim(tasks, caps, current, fake_orchestrator(calls)))
    tasks.finish(current, result)
    assert calls == {'intent': 1, 'agent': 1}
    assert caps.governance.read(task['id'], ALICE)['budget']['used_tokens'] == 10
    assert len([e for e in tasks.get(task['id'], ALICE)['events'] if e['kind'] == 'assistant']) == 1
    assert tasks.get(task['id'], ALICE)['runs'][0]['status'] == 'done'


def test_lease_and_input_fences_prevent_stale_checkpoint_writes(env, caps):
    tasks, _, clock = env
    task = room(tasks)
    claim = tasks.claim(5)
    caps.plan_store.save(claim, 'one', {'v': 1}, {'answer': 1}, {'input_tokens': 3})
    caps.plan_store.save(claim, 'one', {'v': 1}, {'answer': 1}, {'input_tokens': 3})
    assert caps.governance.read(task['id'], ALICE)['budget']['used_tokens'] == 3
    with pytest.raises(TaskConflict):
        caps.plan_store.read(claim, 'one', {'v': 2})
    with pytest.raises(TaskConflict):
        caps.plan_store.save(claim, 'one', {'v': 1}, {'answer': 2}, {})
    clock.t += 6
    with pytest.raises(LeaseLostError):
        caps.plan_store.save(claim, 'late', {}, {}, {})
    current = tasks.claim(5)
    assert caps.plan_store.read(current, 'one', {'v': 1}) == {'answer': 1}
    tasks.stop(task['id'], ALICE)
    with pytest.raises(LeaseLostError):
        caps.plan_store.save(current, 'late', {}, {}, {})
    assert all('payload' not in row for row in caps.plan_store.list(task['id'], ALICE))


def test_checkpoint_and_usage_rollback_if_lease_expires_during_commit(env, caps, monkeypatch):
    tasks, _, clock = env
    task = room(tasks)
    claim = tasks.claim(5)
    original = caps.governance.charge

    def expire(*args):
        original(*args)
        clock.t += 6

    monkeypatch.setattr(caps.governance, 'charge', expire)
    with pytest.raises(LeaseLostError):
        caps.plan_store.save(claim, 'one', {}, {}, {'input_tokens': 10})
    assert caps.governance.read(task['id'], ALICE)['budget']['used_tokens'] == 0
    with tasks.engine.connect() as db:
        assert not db.execute(select(task_checkpoints)).first()


def test_failed_agent_step_is_retried_and_its_known_usage_is_charged(env, caps):
    tasks, _, clock = env
    task = room(tasks)
    calls = Counter()
    orchestrator = fake_orchestrator(calls)
    execute = orchestrator._execute

    async def fail(req):
        result = await execute(req)
        result.success, result.status = False, 'failed'
        return result

    orchestrator._execute = fail
    first = tasks.claim()
    with pytest.raises(RuntimeError):
        asyncio.run(run_claim(tasks, caps, first, orchestrator))
    tasks.fail(first, 'model_failure')
    clock.t += 3
    second = tasks.claim()
    tasks.finish(second, asyncio.run(run_claim(tasks, caps, second, fake_orchestrator(calls))))
    assert calls['agent'] == 2
    assert caps.governance.read(task['id'], ALICE)['budget']['used_tokens'] == 20


def test_model_claim_is_not_verified_success_and_replans_are_bounded(env, caps):
    tasks, _, _ = env
    task = ready_task(tasks, caps, limit=2)
    worker = TaskVerificationWorker(caps.goals)
    for i in range(3):
        assert asyncio.run(worker.run_once())
        view = caps.goals.read(task['id'], ALICE)
        assert not view['acceptable']
        assert view['goal']['replans_used'] == min(i + 1, 2)
        if i < 2:
            claim = tasks.claim()
            event = tasks.context(claim)[1]
            assert event['kind'] == 'verification' and event['data']['replan']
            tasks.finish(claim, {'response': '模型再次声称成功'})
    assert tasks.claim() is None and not asyncio.run(worker.run_once())
    body = tasks.get(task['id'], ALICE)
    assert not body['approvals'] and body['status'] == 'open'
    with pytest.raises(TaskConflict):
        caps.progress.transition(task['id'], ALICE, 'resolved', '模型说完成了')


def test_verification_followup_is_readonly(env, caps):
    tasks, _, _ = env
    task = ready_task(tasks, caps, limit=1)
    asyncio.run(TaskVerificationWorker(caps.goals).run_once())
    claim = tasks.claim()
    orchestrator = fake_orchestrator(Counter())
    execute = orchestrator._execute

    async def inspect(req):
        assert req.allowed_writes == frozenset()
        assert 'request_access' in req.hidden_tools
        with pytest.raises(ValueError):
            await req.before_unsafe_tool('request_access')
        return await execute(req)

    orchestrator._execute = inspect
    tasks.finish(claim, asyncio.run(run_claim(tasks, caps, claim, orchestrator)))
    assert not tasks.get(task['id'], ALICE)['approvals']


def test_fresh_current_entitlement_gates_human_acceptance(env, caps):
    tasks, workflow, clock = env
    task = ready_task(tasks, caps)
    adapter = LocalDirectoryAdapter(workflow.engine)
    run_in_tenant(adapter.apply('grant', {'tool_name': 'request_access', 'arguments': {'employee_id': ALICE.employee_id, 'system': 'gitlab', 'role': 'readonly'}}))
    worker = TaskVerificationWorker(caps.goals)
    assert asyncio.run(worker.run_once())
    assert caps.goals.read(task['id'], ALICE)['acceptable']
    assert tasks.get(task['id'], ALICE)['status'] == 'open'  # verification cannot accept for a human
    clock.t += 301
    with pytest.raises(TaskConflict):
        caps.progress.transition(task['id'], ALICE, 'resolved', '通过')
    caps.goals.request(task['id'], ALICE, 'fresh')
    asyncio.run(worker.run_once())
    caps.progress.transition(task['id'], ALICE, 'resolved', '申请人实际访问并确认')
    assert tasks.get(task['id'], ALICE)['status'] == 'resolved'


def test_verifier_checks_current_state_not_historical_operation(env, caps):
    tasks, workflow, _ = env
    task = ready_task(tasks, caps)
    adapter = LocalDirectoryAdapter(workflow.engine)
    args = {'employee_id': ALICE.employee_id, 'system': 'gitlab', 'role': 'readonly'}
    run_in_tenant(adapter.apply('grant', {'tool_name': 'request_access', 'arguments': args}))
    run_in_tenant(adapter.apply('revoke', {'tool_name': 'revoke_access', 'arguments': {**args, 'restore_to': None}}))
    assert run_in_tenant(adapter.observe('grant', {}))
    asyncio.run(TaskVerificationWorker(caps.goals).run_once())
    view = caps.goals.read(task['id'], ALICE)
    assert view['history'][0]['status'] == 'failed' and not view['acceptable']


@pytest.mark.parametrize('change', ['goal', 'progress', 'stop', 'message', 'new_check'])
def test_late_verification_cannot_wake_or_accept_changed_task(env, caps, change):
    tasks, _, _ = env
    task = ready_task(tasks, caps, limit=2)
    claim = caps.goals.claim()
    if change == 'goal':
        caps.goals.configure(task['id'], ALICE, 1, CHECKS, 0)
    elif change == 'progress':
        caps.progress.save(task['id'], ALICE, progress(task, task['events'][0]['id'], 1))
    elif change == 'stop':
        tasks.stop(task['id'], ALICE)
    elif change == 'message':
        tasks.post(task['id'], ALICE, '新输入', 'new')
    else:
        caps.goals.request(task['id'], ALICE, 'new')
    assert not caps.goals.settle(claim, [{**CHECKS[0], 'status': 'passed'}])
    assert not caps.goals.read(task['id'], ALICE)['acceptable']
    assert not any(e['data'].get('replan') for e in tasks.get(task['id'], ALICE)['events'])


def test_verifier_lease_expiry_reclaim_and_crash_budget(env, caps):
    tasks, _, clock = env
    task = ready_task(tasks, caps)
    for attempt in range(1, 4):
        claim = caps.goals.claim(5)
        assert claim['attempts'] == attempt
        clock.t += 6
        assert not caps.goals.settle(claim, [{**CHECKS[0], 'status': 'passed'}])
    assert caps.goals.claim() is None
    assert caps.goals.read(task['id'], ALICE)['history'][0]['status'] == 'unknown'


def test_verifier_cas_allows_one_claim_and_one_followup(env, caps):
    tasks, _, _ = env
    task = ready_task(tasks, caps, limit=2)
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda _: caps.goals.claim(), range(2)))
    assert sum(c is not None for c in claims) == 1
    claim = next(c for c in claims if c)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: caps.goals.settle(claim, [{**CHECKS[0], 'status': 'failed'}]), range(2)))
    assert results.count(True) == 1
    assert caps.goals.read(task['id'], ALICE)['goal']['replans_used'] == 1


def test_goal_change_cancels_old_followup_and_checkpoint_authority(env, caps):
    tasks, _, _ = env
    task = ready_task(tasks, caps, limit=1)
    asyncio.run(TaskVerificationWorker(caps.goals).run_once())
    claim = tasks.claim()
    caps.goals.configure(task['id'], ALICE, 1, CHECKS, 0)
    with pytest.raises(LeaseLostError):
        caps.plan_store.save(claim, 'late', {}, {}, {})
    with pytest.raises(LeaseLostError):
        tasks.finish(claim, {'response': '迟到'})
    assert all(a['status'] != 'running' for a in caps.governance.read(task['id'], ALICE)['attempts'])


def test_pending_approval_waits_without_spending_replan_budget(env, caps):
    tasks, _, _ = env
    task = room(tasks)
    tasks.finish(tasks.claim(), proposal())
    caps.goals.configure(task['id'], ALICE, 0, CHECKS, 2)
    assert not asyncio.run(TaskVerificationWorker(caps.goals).run_once())
    view = caps.goals.read(task['id'], ALICE)
    assert view['goal']['replans_used'] == 0 and view['history'][0]['attempts'] == 0


def test_verification_acl_input_validation_and_idempotent_http(env, caps, monkeypatch):
    tasks, _, _ = env
    task = room(tasks)
    monkeypatch.setattr(state, 'task_capabilities', caps)
    client = TestClient(app)
    path = '/tasks/' + task['id']
    payload = {'version': 0, 'checks': CHECKS, 'replan_limit': 2}
    assert client.post(path + '/goal', headers=auth('bob'), json=payload).status_code == 403
    assert client.post(path + '/goal', headers=auth(), json={**payload, 'checks': [{**CHECKS[0], 'employee_id': '2001'}]}).status_code == 422
    assert client.post(path + '/goal', headers=auth(), json=payload).status_code == 200
    assert client.post(path + '/goal', headers=auth(), json=payload).status_code == 409
    first = client.post(path + '/verify', headers=auth(), json={'client_request_id': 'same'})
    assert first.status_code == 200
    assert client.post(path + '/verify', headers=auth(), json={'client_request_id': 'same'}).json() == first.json()
    with pytest.raises(TaskForbidden):
        caps.goals.read(task['id'], replace(ALICE, tenant='other'))
    caps.access.observe(BOB)
    caps.access.set_member(task['id'], ALICE, BOB.subject, 'observer')
    assert client.post(path + '/verify', headers=auth('bob'), json={'client_request_id': 'readonly'}).status_code == 403


def test_verification_timeout_does_not_replan_or_claim_success(env, caps):
    tasks, _, _ = env
    task = ready_task(tasks, caps, limit=2)

    async def slow(*args):
        await asyncio.sleep(1)

    caps.goals.verifier.verify = slow
    assert asyncio.run(TaskVerificationWorker(caps.goals, timeout_s=.01).run_once())
    view = caps.goals.read(task['id'], ALICE)
    assert view['history'][0]['status'] == 'unknown' and view['goal']['replans_used'] == 0
    assert tasks.claim() is None


def test_verification_commit_expiry_rolls_back_event_and_replan(env, caps, monkeypatch):
    tasks, _, clock = env
    task = ready_task(tasks, caps, limit=1)
    claim = caps.goals.claim(5)
    original = tasks.record

    def expire(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.t += 6
        return result

    monkeypatch.setattr(tasks, 'record', expire)
    with pytest.raises(LeaseLostError):
        caps.goals.settle(claim, [{**CHECKS[0], 'status': 'failed'}])
    with tasks.engine.connect() as db:
        assert db.execute(select(task_verifications.c.status)).scalar_one() == 'running'
        assert list(db.execute(select(task_runs.c.status)).scalars()) == ['done']
    assert caps.goals.read(task['id'], ALICE)['goal']['replans_used'] == 0


def test_worker_integrates_checkpoint_scope(env, caps):
    tasks, _, _ = env
    task = room(tasks)
    calls = Counter()
    runner = TaskAgentRunner(fake_orchestrator(calls), lambda _: {}, plan_store=caps.plan_store)
    assert asyncio.run(TaskWorker(tasks, runner).run_once())
    assert tasks.get(task['id'], ALICE)['runs'][0]['status'] == 'done'
    assert {'input', 'agent', 'result'} <= {p['step_key'] for p in caps.plan_store.list(task['id'], ALICE)}


def test_restart_after_result_reuses_aggregate_advice_and_usage(env, caps):
    tasks, _, clock = env
    task, calls = room(tasks), Counter()
    orchestrator = fake_orchestrator(calls)

    async def advice(*args):
        calls['advice'] += 1
        return None, {'input_tokens': 3, 'output_tokens': 2}

    runner = TaskAgentRunner(orchestrator, lambda _: {}, advisor=SimpleNamespace(suggest=advice), plan_store=caps.plan_store)

    async def run(claim):
        context, event, transcript = tasks.context(claim)
        context['_claim'] = claim
        return await runner(context, event, transcript)

    first = tasks.claim(5)
    original = asyncio.run(run(first))
    clock.t += 6  # process disappeared before committing the final reply
    second = tasks.claim(5)
    restored = asyncio.run(run(second))
    assert restored == original
    tasks.finish(second, restored)
    assert calls['advice'] == calls['intent'] == 1
    assert calls['agent'] == 1
    assert caps.governance.read(task['id'], ALICE)['budget']['used_tokens'] == 15
