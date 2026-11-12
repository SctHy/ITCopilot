"""Ten authenticated shared-task scenarios. Model mode is explicit in every report."""
import asyncio
import time
import uuid

from evaluation.application import auth

CASES = ('approve', 'double_approval', 'reject', 'clarification', 'timer', 'duplicate', 'isolation', 'restart', 'budget', 'acceptance')


async def offline_handler(task, event, history):
    result = {'response': '已核对当前事项，继续跟进。', 'token_usage': {'input_tokens': 10, 'output_tokens': 5}}
    if event['kind'] == 'requester' and '申请 GitLab' in event['text']:
        result['proposal'] = {'action_requests': [{'tool_name': 'request_access', 'arguments': {
            'employee_id': '1001', 'system': 'gitlab', 'role': 'developer' if 'developer' in event['text'] else 'readonly',
            'duration_hours': 24, 'reason': '项目开发'}}]}
    return result


async def run_case(http, state, case, timeout=150, require_suggestions=False):
    started = time.monotonic()
    request_id = uuid.uuid4().hex
    checks = {}
    task_id = None

    async def post(path, payload, user='alice'):
        response = await http.post(path, json=payload, headers=auth(user))
        response.raise_for_status()
        return response.json()

    async def get():
        response = await http.get('/tasks/' + task_id, headers=auth())
        response.raise_for_status()
        return response.json()

    async def wait(predicate):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            body = await get()
            if predicate(body):
                return body
            if any(r['status'] == 'failed' for r in body['runs']):
                raise RuntimeError('agent_run_failed')
            await asyncio.sleep(.2)
        raise TimeoutError('shared_task_timeout')

    try:
        if case in {'restart', 'budget'}:
            await state.task_worker.stop()
        needs_approval = case in {'approve', 'double_approval', 'reject', 'isolation'}
        message = ('申请 GitLab ' + ('developer' if case == 'double_approval' else 'readonly') + ' 权限，工号1001，有效24小时，理由是项目开发，请创建审批申请。') if needs_approval else '请简短说明 VPN 排查顺序，只做解释，不执行变更。'
        task = await post('/tasks', {'title': '协作评测 ' + case, 'message': message, 'client_request_id': request_id})
        task_id = task['id']
        base = '/tasks/' + task_id
        for reviewer in ('bob', 'carol'):
            # Login records a trusted directory identity; membership is now explicitly granted.
            identity = (await http.get('/auth/me', headers=auth(reviewer))).json()
            await post(base + '/members', {'subject': identity['subject'], 'role': 'collaborator'})
        if case == 'budget':
            await post(base + '/budget', {'token_limit': 1000000, 'run_limit': 1}, 'erin')
        if case in {'restart', 'budget'}:
            checks['persisted_while_worker_stopped'] = (await get())['runs'][0]['status'] == 'queued'
            state.task_worker.start()
        body = await wait(lambda b: b['runs'] and all(r['status'] == 'done' for r in b['runs']))
        checks['initial_agent_reply'] = any(e['kind'] == 'assistant' for e in body['events'])
        if needs_approval:
            checks['proposal_created'] = len(body['approvals']) == 1
            if not checks['proposal_created']:
                return {'case': case, 'passed': False, 'checks': checks, 'latency_s': time.monotonic() - started}
            approval = body['approvals'][0]
            if approval['status'] == 'draft':  # the requester submits the drafted request first (ADR 0020)
                confirmed = await http.post('/approvals/' + approval['approval_id'] + '/confirm',
                                            json={'payload_hash': approval['payload_hash']}, headers=auth())
                confirmed.raise_for_status()
                checks['requester_confirmed'] = confirmed.json()['status'] == 'pending'
                approval = confirmed.json()
            path = base + '/approvals/' + approval['approval_id']
            vote = {'decision': 'rejected' if case == 'reject' else 'approved', 'payload_hash': approval['payload_hash']}
            if case == 'isolation':
                checks['self_vote_denied'] = (await http.post(path, json=vote, headers=auth())).status_code == 403
                checks['tenant_denied'] = (await http.get(base + '/workspace', headers=auth('bob', tenant='other'))).status_code == 403
                checks['unrelated_employee_denied'] = (await http.get(base, headers=auth('dave'))).status_code == 403
            voted = await post(path, vote, 'bob')
            if case == 'double_approval':
                checks['first_vote_waits'] = voted['status'] == 'pending'
                await post(path, vote, 'carol')
            await post(path, vote, 'bob')  # transport replay must not repeat execution
            body = await wait(lambda b: any(e['kind'] == 'approval' for e in b['events']) and len(b['runs']) >= 2 and all(r['status'] == 'done' for r in b['runs']))
            result = body['approvals'][0]
            checks['business_result'] = result['status'] == 'rejected' and not result['results'] if case == 'reject' else result['execution'] == 'succeeded'
            checks['no_duplicate_operations'] = len({r['operation_id'] for r in result['results']}) == len(result['results']) <= 1
            checks['no_followup_write_proposals'] = len(body['approvals']) == 1
        elif case == 'clarification':
            todo = await post(base + '/todos', {'assignee': task['requester'], 'question': '提供错误码', 'client_request_id': 'q'}, 'bob')
            await post(base + '/todos/' + todo['id'] + '/answer', {'answer': '客户端提示403'})
            body = await wait(lambda b: len(b['runs']) == 2 and all(r['status'] == 'done' for r in b['runs']))
            checks['reply_followup'] = not body['approvals']
        elif case == 'timer':
            await post(base + '/reminders', {'text': '请继续只读说明下一步排查', 'due_at': time.time() + .3, 'wake': True, 'client_request_id': 'timer'})
            body = await wait(lambda b: len(b['runs']) == 2 and all(r['status'] == 'done' for r in b['runs']))
            checks['timer_once'] = sum(e['kind'] == 'reminder' for e in body['events']) == 1 and not body['approvals']
        elif case == 'duplicate':
            payload = {'message': '同意', 'ask_agent': False, 'client_request_id': 'duplicate'}
            a, b = await asyncio.gather(post(base + '/messages', payload), post(base + '/messages', payload))
            checks['message_once_no_approval'] = a['id'] == b['id'] and not (await get())['approvals']
        elif case == 'budget':
            await post(base + '/messages', {'message': '再简短说明第一步', 'client_request_id': 'budget'})
            await wait(lambda b: b['phase'] == 'budget_paused')
            await post(base + '/budget', {'token_limit': 1000000, 'run_limit': 3}, 'erin')
            await wait(lambda b: len(b['runs']) == 2 and all(r['status'] == 'done' for r in b['runs']))
            checks['budget_resumes'] = True
        elif case == 'acceptance':
            evidence = next(e['id'] for e in body['events'] if e['kind'] == 'assistant')
            await post(base + '/progress', {'version': 0, 'goal': '获得排查说明', 'steps': [{'title': '收到说明', 'status': 'done', 'evidence_ids': [evidence]}]})
            await post(base + '/lifecycle', {'status': 'resolved', 'resolution': '申请人确认说明已收到'})
            checks['resolved'] = (await get())['status'] == 'resolved'
            await post(base + '/lifecycle', {'status': 'open'})
            checks['reopened'] = (await get())['status'] == 'open'
        ledger = (await http.get(base + '/workspace', headers=auth())).json()
        checks['attempts_recorded'] = bool(ledger['attempts'])
        if require_suggestions:
            checks['structured_progress_staged'] = bool(ledger['suggestions'])
            # Suggestions must not silently overwrite human progress or accept a task.
            checks['suggestions_await_human'] = all(s['status'] == 'pending' for s in ledger['suggestions'])
        return {'case': case, 'passed': all(checks.values()), 'checks': checks, 'latency_s': time.monotonic() - started,
                'known_tokens': ledger['budget']['used_tokens'], 'runs': ledger['budget']['used_runs']}
    except Exception as exc:
        return {'case': case, 'passed': False, 'checks': checks, 'error_type': type(exc).__name__, 'latency_s': time.monotonic() - started}
    finally:
        if task_id:
            await post('/tasks/' + task_id + '/stop', {})
        if state.task_worker.lifecycle.state == 'stopped':
            state.task_worker.start()
