"""Replay source-fidelity and follow-up checks against a running local server.

Requires --live: creates explicitly simulated shared tasks and uses real model calls.
Semantic reply quality still requires reviewing the saved responses, not keyword scoring.
"""
import argparse
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx


CASES = {
    '403': [
        '【验收模拟】我是 alice，工号1001。我在家庭网络且没连 VPN，用 Chrome 130 无痕窗口登录 alice，打开 engineering/platform 仓库及成员页都只显示“403 Forbidden”，无其他文字，今天10:00发生。bob 在公司办公网能打开。我只需要读代码。所以我肯定没加入项目，对吗？请简短分析并给最少的只读排查建议；全程仅为假设，不创建工单、权限申请或其他变更。',
        '【验收模拟补充】alice 保持同一个账号、浏览器和同一仓库地址，只切换到公司办公网，现在项目首页、成员页和代码都能打开。成员页显示 alice 为 Reporter。她仍只需读代码。现在能确定是家庭网络问题了吗？请简短说明判断与下一步，继续只读。',
        '【验收模拟更正】更正我上一条：alice 在公司办公网能打开的是项目首页和成员页，点击仓库代码仍然显示“403 Forbidden”，没有其他文字；我刚才说代码能打开不准确。成员页仍显示 alice 为 Reporter。账号、浏览器、仓库路径都不变。请更新判断，指出仍缺的关键证据，并给出最少的下一步；继续只读，不创建任何申请。',
        '【验收模拟】收到，没有新增信息，继续等待核查，目标和下一步均不变。只需简短回应，不再追问，也不新增步骤或变更申请。',
    ],
    'vpn': [
        '【演示假设，不是真实故障】我是 alice（1001），在家庭网络用公司 VPN 客户端报错，完整文字只有“691”，没有其他文字，也没有截图且无法提供。昨天未改密码。是不是账号已经锁定了？请只读分析，不执行重置、解锁、申请或其他变更。',
        '【演示补充】我是协作者 bob。alice 确认公司 SSO 网页能登录，但同一账号 VPN 仍报691。刚才“昨天未改密码”不准确，更正为昨天改过密码，VPN 仍使用旧的已保存密码；没有其他错误文字，无法提供截图。请更新判断并只给一个下一步，不执行变更。',
        '【演示】暂时没有新结果，也无法补充资料，继续等待 alice 自行确认。保持原进度，只简短回应，不新增问题或操作。',
    ],
}


def run(args):
    report = {'suite': 'shared-brief-fidelity', 'transport': 'local HTTP', 'mode': 'real provider',
              'scope': 'Simulated scenarios; mechanical checks plus separate manual semantic review.',
              'semantic_review': 'pending', 'cases': []}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    with httpx.Client(base_url=args.base_url, timeout=60) as client:
        headers = {}
        for user in ('alice', 'bob'):
            response = client.post('/auth/dev-token', json={'username': user})
            response.raise_for_status()
            headers[user] = {'Authorization': 'Bearer ' + response.json()['access_token']}
            client.get('/auth/me', headers=headers[user]).raise_for_status()

        def request(method, path, body=None, user='alice'):
            response = client.request(method, path, headers=headers[user], json=body)
            response.raise_for_status()
            return response.json()

        for name in args.cases.split(','):
            messages = CASES[name]
            case = {'name': name, 'rounds': []}
            report['cases'].append(case)
            task = request('POST', '/tasks', {'title': '验收：摘要保真 ' + name,
                'message': messages[0], 'client_request_id': uuid.uuid4().hex})
            base = '/tasks/' + task['id']
            case['task_id'] = task['id']
            request('POST', base + '/members', {'subject': 'dev:bob', 'role': 'collaborator'})
            before_ack = None
            for index, message in enumerate(messages):
                started = time.monotonic()
                if index:
                    request('POST', base + '/messages', {'message': message,
                        'client_request_id': uuid.uuid4().hex}, user='bob')
                deadline = time.monotonic() + 180
                while True:
                    task = request('GET', base)
                    if any(r['status'] == 'failed' for r in task['runs']):
                        case['error'] = 'agent run failed'
                        save()
                        raise RuntimeError(case['error'])
                    if len(task['runs']) == index + 1 and all(r['status'] == 'done' for r in task['runs']):
                        break
                    if time.monotonic() > deadline:
                        save()
                        raise TimeoutError('agent turn did not finish')
                    time.sleep(1)
                workspace = request('GET', base + '/workspace')
                reply = [e for e in task['events'] if e['kind'] == 'assistant'][-1]
                brief = workspace['brief'] or {}
                sources = {e['id']: e['text'] for e in task['events']}
                checks = {
                    'original_scope_preserved': brief.get('origin', {}).get('quote') == messages[0],
                    'reply_reviewed': reply['data'].get('reply_reviewed') is True,
                    'fresh_brief': brief.get('event_id') == reply['id'],
                    'user_information_verbatim': all(line in '\n'.join(sources[s['event_id']] for s in item['sources'])
                        for category in ('reported', 'answered') for item in brief.get(category, [])
                        for line in item['text'].splitlines()),
                    'no_approval': not task['approvals'],
                    'nonzero_usage': all(reply['data']['token_usage'][k] > 0 for k in ('input_tokens', 'output_tokens')),
                    'distinct_user_items': all(len(items) == len({
                        (item['text'], tuple(sorted({s['event_id'] for s in item['sources']}))) for item in items})
                        for items in (brief.get('reported', []), brief.get('answered', []))),
                }
                if index == 0:
                    checks['initial_followup_present'] = bool(brief.get('questions'))
                if before_ack is not None:
                    checks.update(progress_unchanged=workspace['progress'] == before_ack['progress'],
                                  no_extra_suggestion=len(workspace['suggestions']) == len(before_ack['suggestions']),
                                  brief_unchanged_on_ack=all(brief.get(category) == before_ack['brief'].get(category)
                                      for category in ('reported', 'answered', 'hypotheses', 'questions')))
                row = {'input': message, 'response': reply['text'], 'brief': brief, 'checks': checks,
                       'usage': reply['data']['token_usage'], 'elapsed_s': round(time.monotonic() - started, 2),
                       'progress': workspace['progress'], 'suggestions': workspace['suggestions']}
                case['rounds'].append(row)
                save()
                print(json.dumps({'case': name, 'round': index + 1, 'checks': checks,
                    'response': reply['text'], 'questions': [q['text'] for q in brief.get('questions', [])]}, ensure_ascii=False), flush=True)
                if index == len(messages) - 2:
                    pending = [s for s in workspace['suggestions'] if s['status'] == 'pending']
                    if pending:
                        request('POST', base + '/suggestions/' + pending[0]['id'], {'accept': True})
                    before_ack = request('GET', base + '/workspace')
        report['mechanical_checks_passed'] = all(all(row['checks'].values()) for case in report['cases'] for row in case['rounds'])
        save()
    return 0 if report['mechanical_checks_passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:8001')
    parser.add_argument('--cases', default=','.join(CASES))
    parser.add_argument('--output', default='evaluation/reports/shared_brief_fidelity.json')
    args = parser.parse_args()
    if urlparse(args.base_url).hostname not in {'127.0.0.1', 'localhost', '::1'}:
        parser.error('Only a local development server is supported')
    if any(case not in CASES for case in args.cases.split(',')):
        parser.error('Unknown case')
    raise SystemExit(run(args))
