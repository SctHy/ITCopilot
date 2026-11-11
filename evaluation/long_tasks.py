"""Deterministic fault experiments using the real task, checkpoint, and verification services."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from agents.agent_orchestrator import AgentOrchestrator
from agents.intent.taxonomy import IntentCategory, UrgencyLevel
from agents.planner import ExecutionPlan, PlanStep
from agents.task_runner import TaskAgentRunner
from agents.types import AgentResponse, AgentType
from core.identity import Principal
from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from workflow.goal_verifiers import AccessGoalVerifier
from workflow.store import WorkflowStore
from workflow.task_capabilities import TaskCapabilities
from workflow.task_ports import CompositeTaskEffects
from workflow.task_verification import TaskVerificationWorker
from workflow.tasks import TaskStore, TaskConflict

APPLICANT = Principal('eval:applicant', 'applicant', 'engineering', frozenset({'employee'}), '1001')
CHECKS = [{'kind': 'access_level', 'system': 'gitlab', 'role': 'readonly'}]


def compose(url, clock):
    workflow = WorkflowStore(url, clock=clock)
    tasks = TaskStore(workflow.engine, clock=clock)
    caps = TaskCapabilities(tasks, lambda task: {}, AccessGoalVerifier(AdapterRegistry(LocalDirectoryAdapter(workflow.engine))))
    tasks.governance = caps.governance
    tasks.effects = CompositeTaskEffects([tasks.effects, caps.goals])
    return workflow, tasks, caps


async def child(url, phase, calls_path, checkpoints):
    """Parent SIGKILLs at the start of step two, after step one's durable save."""
    workflow, tasks, caps = compose(url, lambda: 1000.0 if phase == 'crash' else 1006.0)
    orchestrator = AgentOrchestrator.__new__(AgentOrchestrator)
    orchestrator._planning_mode = 'bounded'

    async def recognize(*args, **kwargs):
        return SimpleNamespace(intent=IntentCategory.ACCESS_REVIEW, intent_group='access', confidence=1,
            urgency=UrgencyLevel.LOW, entities={}, missing_slots=[], requested_actions=frozenset())

    async def plan(*args):
        return ExecutionPlan('fault-plan', '只读诊断', [PlanStep('access', '权限诊断', 'access', []),
            PlanStep('network', '访问诊断', 'network_software', ['access'])])

    async def execute(req, kind):
        with Path(calls_path).open('a') as output:
            output.write(json.dumps({'phase': phase, 'step': kind.value}) + '\n')
            output.flush()
            os.fsync(output.fileno())
        if phase == 'crash' and kind == AgentType.NETWORK_SOFTWARE:
            print('FAULT_POINT', flush=True)
            await asyncio.Event().wait()
        return AgentResponse(kind, '确定性诊断结果', True, token_usage={'input_tokens': 7, 'output_tokens': 3})

    orchestrator.recognize_intent, orchestrator._planner, orchestrator._execute = recognize, SimpleNamespace(plan=plan), execute
    try:
        claim = tasks.claim(5)
        if not claim:
            raise RuntimeError('expected a recoverable task')
        task, event, transcript = tasks.context(claim)
        task['_claim'] = claim
        runner = TaskAgentRunner(orchestrator, lambda _: {}, plan_store=caps.plan_store if checkpoints else None)
        result = await runner(task, event, transcript)
        tasks.finish(claim, result)
    finally:
        workflow.close()


async def recovery_trial(directory, checkpoints):
    url = 'sqlite:///' + str(directory / 'task.sqlite3')
    calls_path = directory / 'calls.jsonl'
    workflow, tasks, caps = compose(url, lambda: 1000.0)
    task = tasks.create(APPLICANT, '故障恢复', '只读检查权限和网络', 'crash')
    workflow.close()
    script = Path(__file__).resolve().parents[1] / 'scripts/evaluate_long_tasks.py'
    command = [sys.executable, str(script), '--database', url, '--calls-path', str(calls_path), '--checkpoints', str(int(checkpoints))]
    process = await asyncio.create_subprocess_exec(*command, '--child', 'crash', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        marker = await asyncio.wait_for(process.stdout.readline(), 30)
        if marker.strip() != b'FAULT_POINT':
            raise RuntimeError('child failed before fault point: ' + (await process.stderr.read()).decode()[-2000:])
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()
    resumed = await asyncio.create_subprocess_exec(*command, '--child', 'resume', stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, errors = await asyncio.wait_for(resumed.communicate(), 30)
    except BaseException:
        if resumed.returncode is None:
            resumed.kill()
        await resumed.communicate()
        raise
    if resumed.returncode:
        raise RuntimeError(errors.decode()[-2000:])
    workflow, tasks, caps = compose(url, lambda: 1006.0)
    try:
        body = tasks.get(task['id'], APPLICANT)
        calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
        first_calls = sum(row['step'] == 'access' for row in calls)
        replies = sum(e['kind'] == 'assistant' for e in body['events'])
        return {'scenario': 'sigkill_recovery', 'checkpoints': checkpoints,
                'passed': body['runs'][0]['status'] == 'done' and replies == 1 and first_calls == (1 if checkpoints else 2),
                'recovered': body['runs'][0]['status'] == 'done', 'assistant_replies': replies,
                'specialist_invocations_including_interrupted': len(calls),
                'completed_first_step_invocations': first_calls, 'reused_steps': int(checkpoints and first_calls == 1),
                'human_recovery_actions': 0, 'kill_signal': 'SIGKILL', 'lease_clock': 'logical_1000_to_1006'}
    finally:
        workflow.close()


async def verification_trial(directory, scenario):
    workflow, tasks, caps = compose('sqlite:///' + str(directory / 'verify.sqlite3'), lambda: 1000.0)
    try:
        task = tasks.create(APPLICANT, '权限达标', '检查权限', scenario)
        tasks.finish(tasks.claim(), {'response': '模型声称已经成功'})
        caps.progress.save(task['id'], APPLICANT, {'version': 0, 'goal': '权限达标',
            'steps': [{'title': '核验', 'status': 'done', 'evidence_ids': [task['events'][0]['id']]}], 'blocker': '', 'next_action': ''})
        caps.goals.configure(task['id'], APPLICANT, 0, CHECKS, 2)
        if scenario == 'actual_entitlement':
            adapter = LocalDirectoryAdapter(workflow.engine)
            await adapter.apply('grant', {'tool_name': 'request_access', 'arguments': {'employee_id': '1001', 'system': 'gitlab', 'role': 'readonly'}})
        if scenario == 'unavailable_verifier':
            async def unavailable(*args):
                raise OSError('injected read failure')
            caps.goals.verifier.verify = unavailable
        worker = TaskVerificationWorker(caps.goals)
        for _ in range(4):
            if not await worker.run_once():
                break
            claim = tasks.claim()
            if claim:
                tasks.finish(claim, {'response': '确定性只读诊断；没有业务写入'})
        view = caps.goals.read(task['id'], APPLICANT)
        before = tasks.get(task['id'], APPLICANT)['status']
        accepted = True
        try:
            caps.progress.transition(task['id'], APPLICANT, 'resolved', '人工验收')
        except TaskConflict:
            accepted = False
        expected = scenario == 'actual_entitlement'
        expected_replans = 2 if scenario == 'false_completion' else 0
        return {'scenario': scenario, 'passed': accepted == expected and before == 'open' and view['goal']['replans_used'] == expected_replans,
                'human_acceptance_allowed': accepted, 'false_acceptance': accepted and not expected,
                'automatic_acceptance': before != 'open', 'readonly_replans': view['goal']['replans_used'],
                'verification_count': len(view['history']), 'human_handoff_required': not accepted}
    finally:
        workflow.close()
