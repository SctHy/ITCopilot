"""Discussion summaries must retain provenance without changing approval or progress authority."""
import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from api import state
from api.main import app
from core.concurrency import LeaseLostError
from tests.conftest import auth
from tests.test_task_product import env as shared_env, product as shared_product
from tests.test_shared_tasks import room
from tests.test_workflow import ALICE, BOB
from workflow.tasks import TaskConflict, TaskForbidden, key

env = shared_env
product = shared_product


def item(event, text=None):
    return {'text': text or event['text'], 'sources': [{'event_id': event['id'], 'quote': event['text']}]}


def suggestion(brief=None, **changes):
    return {'goal': '排查仓库访问', 'steps': [{'title': '核对账号', 'status': 'working', 'evidence_ids': []}],
            'blocker': '等待核对', 'next_action': '核对项目成员关系', 'brief': brief, **changes}


def finish(tasks, claim, payload, version=0):
    tasks.finish(claim, {'response': '只读排查建议', 'progress_suggestion': payload, 'progress_version': version})


def test_summary_updates_without_adopting_progress_and_preserves_sources(env, product, monkeypatch):
    tasks, _, _ = env
    task = room(tasks)
    first = task['events'][0]
    finish(tasks, tasks.claim(), suggestion({'reported': [item(first)], 'questions': [item(first, '仍需确认网络情况')]}))
    brief = product.brief.read(task['id'], BOB)
    assert brief['reported'][0]['sources'][0]['actor_name'] == ALICE.username
    assert product.progress.read(task['id'], ALICE)['version'] == 0
    message = tasks.post(task['id'], BOB, '我们都在公司网络，我能访问，alice 不能。', 'network')
    finish(tasks, tasks.claim(), suggestion({'reported': [item(first), item(message)], 'questions': []}))
    latest = product.brief.read(task['id'], ALICE)
    assert len(latest['reported']) == 2 and latest['questions'] == []
    assert latest['event_id'] != brief['event_id']
    # Identical plans produce one decision even when the discussion summary changes.
    assert len(product.suggestions.list(task['id'], ALICE)) == 1
    assert product.progress.read(task['id'], ALICE)['version'] == 0
    monkeypatch.setattr(state, 'task_capabilities', product)
    response = TestClient(app).get('/tasks/' + task['id'] + '/workspace', headers=auth('bob'))
    assert response.status_code == 200
    assert response.json()['brief'] == latest
    assert 'principal' not in str(latest)


@pytest.mark.parametrize('bad_source', ['foreign', 'future', 'fake_quote', 'assistant_as_fact', 'malformed'])
def test_invalid_summary_cannot_replace_good_snapshot_or_discard_reply(env, product, bad_source):
    tasks, _, _ = env
    task = room(tasks)
    finish(tasks, tasks.claim(), suggestion({'reported': [item(task['events'][0])]}))
    previous = product.brief.read(task['id'], ALICE)
    message = tasks.post(task['id'], ALICE, '继续只读排查', 'next')
    claim = tasks.claim()
    bad = item(message)
    if bad_source == 'foreign':
        other = room(tasks, 'other')
        bad = item(other['events'][0])
    elif bad_source == 'future':
        future = tasks.post(task['id'], BOB, '未来补充', 'future', False)
        bad = item(future)
    elif bad_source == 'fake_quote':
        bad['sources'][0]['quote'] = '不存在的原话'
    elif bad_source == 'assistant_as_fact':
        bad = {'text': '已确认根因', 'sources': [{'event_id': key('reply', message['id']), 'quote': '只读排查建议'}]}
    elif bad_source == 'malformed':
        bad = {'text': '没有引用', 'sources': []}
    finish(tasks, claim, suggestion({'reported': [bad]}))
    assert product.brief.read(task['id'], ALICE) == previous
    reply = tasks.get(task['id'], ALICE)['events'][-1]
    assert reply['kind'] == 'assistant' and reply['data']['brief_unavailable']


def test_summary_scope_history_window_and_causal_boundary(env, product):
    tasks, _, _ = env
    task = room(tasks)
    initial = task['events'][0]
    claim = tasks.claim()
    # A message sent while the first turn is still running must not see a future summary.
    next_event = tasks.post(task['id'], ALICE, '继续', 'next')
    finish(tasks, claim, suggestion({'reported': [item(initial)]}))
    assert product.brief.context(task['id'], next_event['seq']) is None
    tasks.finish(tasks.claim(), {'response': '继续排查'})
    for i in range(35):
        tasks.post(task['id'], BOB, f'补充 {i}', str(i), False)
    latest = tasks.post(task['id'], ALICE, '查看已有信息', 'latest')
    summary = product.brief.context(task['id'], latest['seq'])
    assert summary['reported'][0]['sources'][0]['quote'] == initial['text']
    assert 'principal' not in str(summary)
    for outsider in [replace(BOB, tenant='other'), replace(ALICE, subject='uninvited')]:
        with pytest.raises(TaskForbidden):
            product.brief.read(task['id'], outsider)


def test_summary_is_atomic_with_run_lease_and_does_not_change_acceptance(env, product):
    tasks, _, clock = env
    task = room(tasks)
    claim = tasks.claim(5)
    clock.t += 6
    with pytest.raises(LeaseLostError):
        finish(tasks, claim, suggestion({'reported': [item(task['events'][0])]}))
    assert product.brief.read(task['id'], ALICE) is None
    finish(tasks, tasks.claim(), suggestion({'reported': [item(task['events'][0])]}))
    with pytest.raises(TaskConflict):
        product.progress.transition(task['id'], ALICE, 'resolved', '摘要说完成')


def test_new_plan_supersedes_old_but_noop_needs_no_new_decision(env, product):
    tasks, _, _ = env
    task = room(tasks)
    finish(tasks, tasks.claim(), suggestion())
    original = product.suggestions.list(task['id'], ALICE)[0]
    tasks.post(task['id'], ALICE, '补充', 'change')
    finish(tasks, tasks.claim(), suggestion(blocker='', next_action='检查只读角色'))
    rows = product.suggestions.list(task['id'], ALICE)
    assert sum(row['status'] == 'pending' for row in rows) == 1
    assert next(row for row in rows if row['id'] == original['id'])['status'] == 'superseded'
    with pytest.raises(TaskConflict):
        product.suggestions.decide(task['id'], original['id'], ALICE, True)
    current = next(row for row in rows if row['status'] == 'pending')
    product.suggestions.decide(task['id'], current['id'], ALICE, True)
    accepted = product.progress.read(task['id'], ALICE)
    tasks.post(task['id'], ALICE, '重复信息', 'same')
    finish(tasks, tasks.claim(), suggestion(blocker='', next_action='检查只读角色'), accepted['version'])
    assert len(product.suggestions.list(task['id'], ALICE)) == 2
    assert product.progress.read(task['id'], ALICE)['version'] == accepted['version']


def test_runner_receives_brief_but_collaborator_turn_stays_readonly(env, product):
    from agents.task_runner import TaskAgentRunner
    from tests.test_task_execution import fake_orchestrator
    from collections import Counter
    tasks, _, _ = env
    task = room(tasks)
    finish(tasks, tasks.claim(), suggestion({'reported': [item(task['events'][0])]}))
    tasks.post(task['id'], BOB, '同一网络，继续检查', 'followup')
    context, event, transcript = tasks.context(tasks.claim())
    runner = TaskAgentRunner(fake_orchestrator(Counter()), lambda _: {}, product.progress.context,
        read_brief=product.brief.context)
    request, progress = asyncio.run(runner._prepare(context, event, transcript))
    assert progress['brief']['reported']
    assert '用户陈述未经独立核验' in request.context
    assert f"发言人：{BOB.username}；事件类型：{event['kind']}" in request.context
    assert request.principal.subject == ALICE.subject
    assert not request.allowed_writes
    assert progress['discussion_evidence'][-1]['id'] == event['id']


def test_structured_advisor_receives_prior_facts_and_current_source_ids(env, product):
    from agents.task_progress_advisor import TaskProgressAdvisor
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    tasks, _, _ = env
    task = room(tasks)
    first = task['events'][0]
    finish(tasks, tasks.claim(), suggestion({'reported': [item(first)], 'questions': [item(first, '网络情况？')]}))
    message = tasks.post(task['id'], BOB, '两人都在公司网络，只有 alice 不能访问。', 'network')
    claim = tasks.claim()
    progress = product.progress.context(task['id'])
    progress['brief'] = product.brief.context(task['id'], message['seq'])
    progress['discussion_evidence'] = [{'id': message['id'], 'kind': message['kind'], 'actor_name': BOB.username, 'text': message['text']}]

    def request(messages, info):
        prompt = str(messages)
        assert 'reported' in prompt and first['id'] in prompt and message['id'] in prompt
        assert '两人都在公司网络' in prompt and '已得到答复的问题' in prompt
        return ModelResponse(parts=[ToolCallPart('draft_progress', suggestion({
            'reported': [item(first), item(message)],
            'hypotheses': [{'text': '可能与账号或角色有关，尚未核实', 'sources': [{'event_id': message['id'], 'quote': message['text']}]}],
            'questions': []
        }, blocker='等待账号与角色核对'))])

    advisor = TaskProgressAdvisor(FunctionModel(request))
    payload, _ = asyncio.run(advisor.suggest(progress, task, message, '只读排查建议', key('reply', message['id'])))
    assert payload is not None
    finish(tasks, claim, payload)
    brief = product.brief.read(task['id'], ALICE)
    assert brief['questions'] == [] and len(brief['reported']) == 2
    assert brief['through_seq'] == message['seq']
    assert product.progress.read(task['id'], ALICE)['version'] == 0


def test_reported_questions_negations_and_original_scope_are_not_rewritten(env, product):
    tasks, _, _ = env
    original = '【验收模拟】仅是假设，不执行变更。' + '背景资料。' * 100 + '我是不是没加入项目？没有其他错误文字。'
    task = tasks.create(ALICE, '原话保真', original, 'verbatim')
    first = task['events'][0]
    draft = {'reported': [
        {'text': 'alice 确认没加入项目', 'sources': [{'event_id': first['id'], 'quote': '没加入项目'}]},
        {'text': '有其他错误文字', 'sources': [{'event_id': first['id'], 'quote': '其他错误文字'}]},
    ]}
    finish(tasks, tasks.claim(), suggestion(draft))
    brief = product.brief.read(task['id'], ALICE)
    assert brief['origin']['quote'] == original
    assert [i['text'] for i in brief['reported']] == ['我是不是没加入项目？', '没有其他错误文字。']
    for i in range(35):
        tasks.post(task['id'], ALICE, f'后续讨论{i}', str(i), False)
    latest = tasks.post(task['id'], ALICE, '继续', 'continue')
    assert product.brief.context(task['id'], latest['seq'])['origin']['quote'] == original


def test_ambiguous_excerpt_preserves_both_question_and_statement():
    from workflow.task_brief import source_sentence
    assert source_sentence('现在能访问吗？原来能访问。', '能访问') == '现在能访问吗？原来能访问。'


def test_excerpts_of_one_answer_do_not_duplicate_the_displayed_sentence():
    from workflow.task_brief import preserve_reported
    evidence = {'a': {'text': '只有691，没有其他文字。'}, 'b': {'text': '只有691，没有其他文字。'}}
    brief = {'reported': [], 'answered': [
        {'text': '草稿', 'sources': [{'event_id': event, 'quote': quote}]}
        for event, quote in [('a', '只有691'), ('a', '没有其他文字'), ('b', '只有691')]]}
    result = preserve_reported(brief, evidence)
    assert len(result['answered']) == 2
    assert [x['sources'][0]['event_id'] for x in result['answered']] == ['a', 'b']
    assert all(x['text'] == evidence['a']['text'] for x in result['answered'])


def test_known_login_identity_is_not_requested_again():
    from types import SimpleNamespace
    from agents.task_progress_advisor import TaskAdvice, validate_readonly_advice
    context = SimpleNamespace(deps={'allow_reply_revision': True, 'discussion_evidence': [
        {'kind': 'requester', 'actor_name': 'alice', 'text': '用无痕窗口登录 alice，仓库显示403。'}]})
    output = TaskAdvice(**{**suggestion(), 'reviewed_response': '只读核对页面显示的实际登录身份，以及响应来源。', 'progress_changed': True})
    assert validate_readonly_advice(context, output).reviewed_response == '登录账号已由用户提供，无需重复确认。'
    output.reviewed_response = '已记录登录 alice。只读查看403的响应来源。'
    assert validate_readonly_advice(context, output).reviewed_response == output.reviewed_response


def test_logging_into_a_service_does_not_supply_a_login_identity():
    from types import SimpleNamespace
    from agents.task_progress_advisor import TaskAdvice, validate_readonly_advice
    context = SimpleNamespace(deps={'allow_reply_revision': True, 'discussion_evidence': [
        {'kind': 'requester', 'actor_name': 'alice', 'text': '登录 GitLab 报403，登录 SSO 也失败。'}]})
    text = '请确认实际登录身份。'
    output = TaskAdvice(**{**suggestion(), 'reviewed_response': text, 'progress_changed': True})
    assert validate_readonly_advice(context, output).reviewed_response == text


def test_reported_role_is_answered_but_new_permission_configuration_can_be_checked():
    from types import SimpleNamespace
    from agents.task_progress_advisor import TaskAdvice, validate_readonly_advice
    context = SimpleNamespace(deps={'allow_reply_revision': True, 'discussion_evidence': [
        {'kind': 'collaborator', 'text': '成员页显示 alice 为 Reporter。'}]})
    output = TaskAdvice(**{**suggestion(), 'reviewed_response': '尚缺：实际授权角色的只读对照结果，目前仅有转述。', 'progress_changed': True})
    assert validate_readonly_advice(context, output).reviewed_response == '成员页角色已由用户提供，无需重复确认。'
    output.reviewed_response = '角色已记录；只读核对网关策略与路径设置是否限制该角色。'
    assert validate_readonly_advice(context, output).reviewed_response == output.reviewed_response


def test_semantic_noop_keeps_pending_and_accepted_progress_despite_rewording(env, product):
    tasks, _, _ = env
    task = room(tasks)
    finish(tasks, tasks.claim(), suggestion())
    first = product.suggestions.list(task['id'], ALICE)[0]
    tasks.post(task['id'], ALICE, '知道了，继续等', 'ack')
    finish(tasks, tasks.claim(), {**suggestion(blocker='暂未核对，继续等回复'), 'progress_changed': False})
    assert [s['id'] for s in product.suggestions.list(task['id'], ALICE)] == [first['id']]
    product.suggestions.decide(task['id'], first['id'], ALICE, True)
    current = product.progress.read(task['id'], ALICE)
    tasks.post(task['id'], ALICE, '仍无新信息', 'ack2')
    finish(tasks, tasks.claim(), {**suggestion(next_action='等候核对角色'), 'progress_changed': False}, current['version'])
    assert len(product.suggestions.list(task['id'], ALICE)) == 1
    assert product.progress.read(task['id'], ALICE) == current
    # A real change still creates a decision; the semantic gate must not hide changed facts.
    tasks.post(task['id'], ALICE, '角色已核对，尚需查网关', 'change')
    finish(tasks, tasks.claim(), {**suggestion(next_action='查网关日志'), 'progress_changed': True}, current['version'])
    assert len(product.suggestions.list(task['id'], ALICE)) == 2


def test_answered_information_requires_human_evidence(env, product):
    tasks, _, _ = env
    task = room(tasks)
    first = task['events'][0]
    finish(tasks, tasks.claim(), suggestion({'answered': [item(first, '用户已经提供需求')]}))
    brief = product.brief.read(task['id'], ALICE)
    assert brief['answered']
    followup = tasks.post(task['id'], ALICE, '继续', 'next')
    finish(tasks, tasks.claim(), suggestion({'answered': [{'text': '模型推断已回答', 'sources': [
        {'event_id': key('reply', followup['id']), 'quote': '只读排查建议'}]}]}))
    assert product.brief.read(task['id'], ALICE) == brief


@pytest.mark.parametrize('write_requested', [False, True])
def test_reply_review_can_correct_readonly_followups_but_never_write_results(env, product, write_requested):
    from agents.task_runner import TaskAgentRunner
    from tests.test_task_execution import fake_orchestrator
    from collections import Counter
    from types import SimpleNamespace
    tasks, _, _ = env
    room(tasks)
    orchestrator = fake_orchestrator(Counter())
    if not write_requested:
        recognize = orchestrator.recognize_intent

        async def readonly(*args, **kwargs):
            result = await recognize(*args, **kwargs)
            result.requested_actions = frozenset()
            return result
        orchestrator.recognize_intent = readonly

    async def review(progress, task, event, reply, evidence_id):
        assert progress['allow_reply_revision'] is not write_requested
        if not write_requested:
            assert progress['tool_observations']['approvals'][0]['status'] == 'pending'
        return {**suggestion(), 'reviewed_response': '已提供完整错误文字，无需重复补充。'}, {}

    runner = TaskAgentRunner(orchestrator, lambda _: {'approval_id': 'actual', 'status': 'pending',
        'execution': 'not_executed', 'confirmed_at': 'now', 'action_requests': []},
        product.progress.context, advisor=SimpleNamespace(suggest=review),
        read_task_state=lambda _: {'approvals': [{'approval_id': 'actual'}]})
    result = asyncio.run(runner(*tasks.context(tasks.claim())))
    assert result['response'] == ('结果' if write_requested else '已提供完整错误文字，无需重复补充。')


def test_readonly_finalizer_uses_primary_evidence_not_a_flawed_draft():
    from agents.task_progress_advisor import TaskProgressAdvisor
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    current = {**suggestion(), 'version': 1, 'allow_reply_revision': True,
        'discussion_evidence': [
            {'id': 'user', 'kind': 'requester', 'text': '完整错误只有403，没有其他文字。'},
            {'id': 'old', 'kind': 'assistant', 'text': 'BAD_OLD_QUESTION'}],
        'tool_observations': {'knowledge': [], 'traces': []}}
    # The correction must survive even at the end of a long latest message.
    event = {'id': 'latest', 'text': '背景。' * 1100 + '更正：没有其他文字。'}

    def model(messages, info):
        prompt = str(messages)
        assert 'BAD_DRAFT' not in prompt and 'BAD_OLD_QUESTION' not in prompt
        assert '更正：没有其他文字。' in prompt
        return ModelResponse(parts=[ToolCallPart('draft_progress', {
            **suggestion(blocker='改了一种措辞'), 'progress_changed': False,
            'reviewed_response': '已记录完整报错，不再索要其他文字。'})])

    output, _ = asyncio.run(TaskProgressAdvisor(FunctionModel(model)).suggest(
        current, {'tenant': 'default', 'title': '排查'}, event, 'BAD_DRAFT', 'reply'))
    assert output['blocker'] == current['blocker']
    assert output['reviewed_response'] == '已记录完整报错，不再索要其他文字。'


@pytest.mark.parametrize('bad_advice', [
    '下一步请更新 VPN 保存的旧密码后重试。',
    '下一步（只读，不执行任何变更）：请把旧密码更新为当前密码。',
])
def test_readonly_credential_mutation_is_retried_before_delivery(bad_advice):
    from agents.task_progress_advisor import TaskProgressAdvisor
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    calls = []

    def model(messages, info):
        calls.append(messages)
        text = bad_advice if len(calls) == 1 else '只读查看客户端日志的错误阶段，不修改凭据。'
        return ModelResponse(parts=[ToolCallPart('draft_progress', {
            **suggestion(), 'progress_changed': False, 'reviewed_response': text})])

    output, _ = asyncio.run(TaskProgressAdvisor(FunctionModel(model)).suggest(
        {**suggestion(), 'allow_reply_revision': True}, {'tenant': 'default', 'title': '只读排查'},
        {'id': 'event', 'text': '只读分析，不执行修改'}, '草稿', 'reply'))
    assert len(calls) == 2
    assert output['reviewed_response'] == '只读查看客户端日志的错误阶段，不修改凭据。'


@pytest.mark.parametrize('bad_advice', ['请查看成员页上显示的角色。',
    '只读对照已授权角色：查看 engineering/platform 的成员/角色信息与仓库实际访问地址是否一致（成员页同样只有403，也记录这一点）。',
    '成员页或授权页上显示的实际登录身份与已授权角色是什么（若该页面本身也返回403、无法显示，请说明）？'])
def test_known_unavailable_page_is_not_requested_again(bad_advice):
    from agents.task_progress_advisor import TaskProgressAdvisor
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    calls = []

    def model(messages, info):
        calls.append(messages)
        text = bad_advice if len(calls) == 1 else '请能访问的协作者查看成员页上显示的角色，不修改权限。'
        return ModelResponse(parts=[ToolCallPart('draft_progress', {
            **suggestion(), 'next_action': text, 'progress_changed': True, 'reviewed_response': text})])

    context = {'allow_reply_revision': True, 'discussion_evidence': [
        {'id': 'u', 'kind': 'requester', 'text': '仓库和成员页都只显示“403 Forbidden”，无法查看角色。'}]}
    output, _ = asyncio.run(TaskProgressAdvisor(FunctionModel(model)).suggest(
        context, {'tenant': 'default', 'title': '只读排查'}, {'id': 'u', 'text': '成员页也打不开'}, '草稿', 'reply'))
    assert len(calls) == 1
    assert '请能访问的协作者' in output['reviewed_response']


def test_two_network_failures_cannot_exclude_network_causes():
    from agents.task_progress_advisor import CAUTIOUS_NETWORK, TaskProgressAdvisor
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    calls = []

    def model(messages, info):
        calls.append(messages)
        text = '网络差异只解释首页，不能解释代码403。' if len(calls) == 1 else '切换网络未恢复代码访问，网络与权限因素都尚未排除。'
        return ModelResponse(parts=[ToolCallPart('draft_progress', {
            **suggestion(), 'progress_changed': True, 'reviewed_response': text})])

    output, _ = asyncio.run(TaskProgressAdvisor(FunctionModel(model)).suggest(
        {'allow_reply_revision': True}, {'tenant': 'default', 'title': '只读排查'},
        {'id': 'u', 'text': '更正：办公网下代码仍403'}, '草稿', 'reply'))
    assert len(calls) == 1
    assert output['reviewed_response'] == CAUTIOUS_NETWORK + '。'


@pytest.mark.parametrize('claim', ['网络路径不是唯一因素。', '网络并非唯一原因。', '网络不是唯一解释。',
    '网络差异不足以解释代码403。', '“家庭网络是根因”的推断不再成立。'])
def test_network_exclusion_variants_stay_unverified(claim):
    from agents.task_progress_advisor import cautious_network_text, CAUTIOUS_NETWORK
    assert cautious_network_text(claim) == CAUTIOUS_NETWORK + '。'


def test_response_source_does_not_determine_root_cause():
    from agents.task_progress_advisor import cautious_network_text
    assert cautious_network_text('这能区分权限问题与网络路径问题。') == '响应来源可提供拦截层级线索，但不能单独确定根因或排除其他因素。'
    assert cautious_network_text('网络与权限因素都尚未排除。') == '网络与权限因素都尚未排除。'


def test_normalizing_a_claim_replaces_its_obsolete_self_quote_with_rendered_text():
    from types import SimpleNamespace
    from agents.task_progress_advisor import TaskAdvice, validate_readonly_advice
    output = TaskAdvice(**{**suggestion({'hypotheses': [{
        'text': '网络差异不能解释代码403。',
        'sources': [{'event_id': 'reply', 'quote': '网络差异不能解释代码403。'}],
    }]}), 'reviewed_response': '网络差异不能解释代码403。', 'progress_changed': True})
    result = validate_readonly_advice(SimpleNamespace(deps={'allow_reply_revision': True, 'response_evidence_id': 'reply'}), output)
    assert result.brief.hypotheses[0].sources[0].quote in result.reviewed_response
    assert '不能解释' not in result.brief.hypotheses[0].sources[0].quote
    assert '不足以排除' in result.reviewed_response


def test_source_id_mismatch_is_corrected_before_persisting_the_brief():
    from agents.task_progress_advisor import TaskProgressAdvisor
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    calls = []

    def model(messages, info):
        calls.append(messages)
        source_id = 'correction' if len(calls) == 1 else 'old'
        return ModelResponse(parts=[ToolCallPart('draft_progress', {
            **suggestion({'hypotheses': [{'text': '旧的成功记录已被更正，不能用来排除网络因素。',
                'sources': [{'event_id': source_id, 'quote': '办公网代码能打开。'}]}]}),
            'progress_changed': True, 'reviewed_response': '已按更正记录，原因仍待核验。'})])

    context = {'allow_reply_revision': True, 'discussion_evidence': [
        {'id': 'old', 'kind': 'requester', 'text': '办公网代码能打开。'},
        {'id': 'correction', 'kind': 'collaborator', 'text': '更正：办公网代码仍403。'}]}
    output, _ = asyncio.run(TaskProgressAdvisor(FunctionModel(model)).suggest(
        context, {'tenant': 'default', 'title': '只读排查'},
        {'id': 'correction', 'text': '更正：办公网代码仍403。'}, '草稿', 'reply'))
    assert len(calls) == 2
    assert output['brief']['hypotheses'][0]['sources'][0]['event_id'] == 'old'


def test_early_quote_check_leaves_truncated_history_to_database_validation():
    from types import SimpleNamespace
    from agents.task_progress_advisor import TaskAdvice, validate_readonly_advice
    output = TaskAdvice(**{**suggestion({'reported': [{
        'text': '更正位于长消息尾部。', 'sources': [{'event_id': 'old', 'quote': '更正位于长消息尾部。'}]}]}),
        'reviewed_response': '以完整原话为准。', 'progress_changed': False})
    context = SimpleNamespace(deps={'allow_reply_revision': True, 'discussion_evidence': [
        {'id': 'old', 'kind': 'requester', 'text': '历史消息前段。', 'truncated': True}]})
    assert validate_readonly_advice(context, output).brief.reported


def test_repaired_reply_and_summary_share_the_same_followups():
    from types import SimpleNamespace
    from agents.task_progress_advisor import TaskAdvice, validate_readonly_advice
    source = {'event_id': 'u', 'quote': '成员页只显示403，无法查看角色。'}
    output = TaskAdvice(**{**suggestion({
        'hypotheses': [{'text': '403 的原因尚未确定。', 'sources': [source]}],
        'questions': [{'text': '成员页上已授权角色是什么？', 'sources': [source]}],
    }), 'reviewed_response': '请查看成员页上显示的角色；若该页同样403，请说明。补充这两项信息后再判断。',
        'progress_changed': True})
    context = SimpleNamespace(deps={'allow_reply_revision': True, 'discussion_evidence': [
        {'id': 'u', 'kind': 'requester', 'text': source['quote']}]})
    result = validate_readonly_advice(context, output)
    assert '请说明' not in result.reviewed_response and '补充这两项' not in result.reviewed_response
    assert result.brief.questions[0].text in result.reviewed_response
    assert '请能访问的协作者' in result.reviewed_response


def test_repairing_self_citations_does_not_discard_all_readonly_next_steps():
    from types import SimpleNamespace
    from agents.task_progress_advisor import TaskAdvice, validate_readonly_advice
    output = TaskAdvice(**{**suggestion({
        'hypotheses': [{'text': '原因尚未确定。', 'sources': [{'event_id': 'reply', 'quote': '原因尚未确定。'}]}],
        'questions': [{'text': '查看成员页角色，以及响应来源。', 'sources': [{'event_id': 'reply', 'quote': '请查看成员页角色。'}]}],
    }), 'reviewed_response': '原因尚未确定。请查看成员页角色。', 'progress_changed': True})
    context = SimpleNamespace(deps={'allow_reply_revision': True, 'response_evidence_id': 'reply',
        'discussion_evidence': [{'id': 'u', 'kind': 'requester', 'text': '成员页只显示403，无法查看角色。'}]})
    result = validate_readonly_advice(context, output)
    assert result.brief.questions and '请能访问的协作者' in result.reviewed_response
    assert result.brief.questions[0].sources[0].quote in result.reviewed_response


def test_member_page_access_keeps_the_network_condition():
    from types import SimpleNamespace
    from agents.task_progress_advisor import TaskAdvice, validate_readonly_advice
    context = SimpleNamespace(deps={'allow_reply_revision': True, 'discussion_evidence': [
        {'kind': 'requester', 'text': '家庭网络仓库与成员页都只显示403。'},
        {'kind': 'collaborator', 'text': '办公网能打开的是项目首页和成员页，代码仍403。'}]})
    output = TaskAdvice(**{**suggestion(), 'reviewed_response': '两种网络下代码页均403、而首页与成员页可访问。', 'progress_changed': True})
    assert validate_readonly_advice(context, output).reviewed_response == '两种网络下代码页均403、而办公网下首页与成员页可访问。'


def test_explicit_no_information_ack_keeps_the_previous_brief():
    from agents.task_progress_advisor import TaskProgressAdvisor
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    old = {'hypotheses': [{'text': '仅办公网成员页可访问。', 'sources': [{'event_id': 'old', 'quote': '办公网成员页可访问。'}]}]}
    context = {**suggestion(), 'allow_reply_revision': True, 'brief': old}

    def model(messages, info):
        return ModelResponse(parts=[ToolCallPart('draft_progress', {
            **suggestion({'hypotheses': [{'text': '成员页可访问。', 'sources': old['hypotheses'][0]['sources']}]}),
            'reviewed_response': '成员页可访问。', 'progress_changed': False})])

    output, _ = asyncio.run(TaskProgressAdvisor(FunctionModel(model)).suggest(
        context, {'tenant': 'default', 'title': '只读排查'},
        {'id': 'u', 'text': '收到，没有新增信息，目标和下一步均不变。'}, '草稿', 'reply'))
    assert output['brief']['hypotheses'] == old['hypotheses']
    assert output['reviewed_response'] == '收到，本轮没有新增信息，进度与待核验项保持不变，继续等待已有核查结果。'


def test_regular_shared_followup_reads_current_approval_after_card_action(env, product):
    from agents.task_runner import TaskAgentRunner
    from agents.checkpointing import encode_request, decode_request
    from tests.test_task_execution import fake_orchestrator
    from collections import Counter
    tasks, _, _ = env
    task = room(tasks)
    finish(tasks, tasks.claim(), suggestion({'reported': [item(task['events'][0])]}))
    tasks.post(task['id'], ALICE, '我在卡片上提交了，好了吗？', 'status')
    context, event, transcript = tasks.context(tasks.claim())
    current = {'approval_id': 'confirmed', 'status': 'pending', 'execution': 'not_executed',
               'confirmed_at': '2026-11-14', 'action_requests': [{'tool_name': 'request_access'}]}
    runner = TaskAgentRunner(fake_orchestrator(Counter()), lambda _: current,
                             read_task_state=lambda _: {'approvals': [{'approval_id': 'confirmed'}]})
    request, _ = asyncio.run(runner._prepare(context, event, transcript))
    assert request.conversation_approvals[0]['status'] == 'pending'
    assert 'pending 是已提交' in request.context
    restored = decode_request(encode_request(request), principal=request.principal)
    assert restored.conversation_approvals == request.conversation_approvals
