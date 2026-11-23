"""A clearly stated write request survives a multi-ask message; questions, refusals and deferrals do not count."""
import asyncio

import pytest

from it_copilot.agent.intent.explicit import explicit_access_systems, explicit_write_requests
from it_copilot.agent.intent.recognizer import IntentRecognizer
from it_copilot.agent.intent.taxonomy import IntentCategory


@pytest.mark.parametrize("message,expected", [
    ("我 VPN 连不上，报错 VPN-691；另外下周要排查 platform 仓库的 CI 失败，帮我申请 GitLab 只读权限，用 3 天。", {"request_access"}),
    ("内部系统登录报 403 是什么原因？另外工号1002，帮我申请 Jira reporter 权限，理由查看看板。", {"request_access"}),
    ("工号1001，账号被锁了请申请解锁；另外 VPN 报 401 错误码是什么意思？", {"unlock_account"}),
    ("工号1002，请发起密码重置申请；同时帮我查一下设备 DEV-011 的资产信息。", {"reset_password"}),
    ("设备DEV-002黑屏，分别检查后请创建一张工单汇总。", {"create_ticket"}),
    ("工号1001，申请 GitLab 只读权限，理由代码审阅；另外显示器闪屏，请顺便建一张工单。", {"request_access", "create_ticket"}),
])
def test_explicit_requests_are_found_in_multi_ask_messages(message, expected):
    assert explicit_write_requests(message) == expected


@pytest.mark.parametrize("message", [
    "怎么申请 GitLab 权限？",
    "GitLab 权限申请流程是什么",
    "我不需要申请权限，只想问 VPN 为什么连不上",
    "工号1001，申请 Jira reporter 权限，理由查看看板。先把要填的内容给我看一下，等我确认再提交。",
    "账号没被锁，不用解锁。",
    "VPN 报 VPN-691 是什么意思？",
    "",
    "仅只读排查，不创建工单、权限申请或其他变更。",
    "不创建工单。",
    "不申请 GitLab 权限。",
    "不执行密码重置申请。",
])
def test_questions_refusals_and_deferrals_are_not_requests(message):
    assert explicit_write_requests(message) == frozenset()


def test_readonly_scope_overrides_inferred_writes_but_keeps_separate_explicit_request():
    async def run():
        recognizer = IntentRecognizer('test', model='test')
        recognizer._llm = lambda message, history: asyncio.sleep(0, result={
            'intent': IntentCategory.PERMISSION_REQUEST, 'confidence': .95, 'entities': {},
            '_llm_used': True, '_source': 'llm_tool_use', '_actions': {'create_ticket', 'request_access'}})
        readonly = await recognizer.recognize('请只读排查，不创建工单、权限申请或其他变更。')
        assert readonly.requested_actions == frozenset()
        mixed = await recognizer.recognize('VPN 请只读排查；另外请创建工单跟踪显示器问题。')
        assert mixed.requested_actions == frozenset({'create_ticket'})
        access = await recognizer.recognize('请申请 GitLab 只读权限。')
        assert 'request_access' in access.requested_actions
    asyncio.run(run())


def test_a_confident_single_intent_no_longer_drops_the_second_explicit_ask():
    async def run():
        recognizer = IntentRecognizer("test", model="test")
        recognizer._llm = lambda message, history: asyncio.sleep(0, result={"intent": IntentCategory.VPN_ISSUE, "confidence": .95, "_actions": set()})
        result = await recognizer.recognize("我 VPN 连不上，报错 VPN-691；另外帮我申请 GitLab 只读权限，用 3 天。")
        assert result.intent == IntentCategory.VPN_ISSUE and result.requested_actions == frozenset({"request_access"})
        plain = await recognizer.recognize("VPN 报 VPN-691 是什么意思？")
        assert plain.requested_actions == frozenset()
    asyncio.run(run())


def test_an_explicit_request_is_not_sent_back_for_vague_clarification():
    async def run():
        recognizer = IntentRecognizer("test", model="test")
        recognizer._llm = lambda message, history: asyncio.sleep(0, result={
            "intent": IntentCategory.OTHER, "confidence": .4, "entities": {}, "_llm_used": True, "_source": "llm_tool_use", "_actions": set()})
        explicit = await recognizer.recognize("工号1002，请发起密码重置申请；同时帮我查一下设备 DEV-011 的资产信息。")
        assert "reset_password" in explicit.requested_actions and not explicit.clarification_needed
        vague = await recognizer.recognize("那个东西又坏了")
        assert vague.clarification_needed and not vague.requested_actions
    asyncio.run(run())


@pytest.mark.parametrize("message,expected", [
    ("VPN 连不上，帮我申请 GitLab 只读权限。", ["GitLab"]),
    ("工号1002，帮我申请 jira reporter 权限；VPN 报 401。", ["Jira"]),
    ("帮我申请 VPN 只读权限。", ["VPN"]),
    ("VPN 连不上，帮我申请只读权限。", []),
    ("怎么申请 GitLab 权限？", []),
    ("不要申请 Jira 权限。", []),
    ("申请 GitLab 权限。等我确认再提交。", []),
    ("申请 GitLab 和 Jira 权限。", []),
    ("申请 GitLab 权限；申请 Jira 权限。", []),
    ("申请 MyGitLab 权限。", []),
    ("申请 GitLab-test 权限。", []),
    ("申请 Salesforce 权限；VPN 连不上。", []),
])
def test_access_target_is_scoped_to_an_unambiguous_explicit_request(message, expected):
    assert explicit_access_systems(message) == expected


@pytest.mark.parametrize("backend", ["llm", "rules"])
def test_permission_request_recovers_missing_system_but_still_requires_real_slots(monkeypatch, backend):
    async def run():
        recognizer = IntentRecognizer("test", model="test")
        if backend == "llm":
            recognizer._llm = lambda message, history: asyncio.sleep(0, result={
                "intent": IntentCategory.PERMISSION_REQUEST, "confidence": .95,
                "entities": {}, "_llm_used": True, "_source": "llm_tool_use", "_actions": set()})
        elif backend == "rules":
            monkeypatch.setenv("IT_INTENT_LLM_ENABLED", "false")
        result = await recognizer.recognize("工号1002，帮我申请 Jira reporter 权限；另外查一下设备 DEV-011。")
        assert result.entities["system"] == ["Jira"]
        assert not result.clarification_needed
        missing_target = await recognizer.recognize("工号1002，帮我申请只读权限；另外查一下设备 DEV-011。")
        assert "system" in missing_target.missing_slots
        missing_employee = await recognizer.recognize("帮我申请 Jira reporter 权限。")
        assert "employee_id" in missing_employee.missing_slots
    asyncio.run(run())


def test_business_intent_does_not_override_explicit_no_new_action():
    async def run():
        recognizer = IntentRecognizer("test", model="test")
        recognizer._llm = lambda *args: asyncio.sleep(0, result={"intent": IntentCategory.PERMISSION_REQUEST, "confidence": .95, "_actions": set()})
        result = await recognizer.recognize("总结一下今天已申请的权限")
        assert result.intent == IntentCategory.PERMISSION_REQUEST
        assert result.requested_actions == frozenset()
    asyncio.run(run())


def test_classifier_keeps_earlier_user_request_across_topic_switches():
    history = [{"role": "user", "content": "GitLab reporter 权限，合并请求评审，先别提交"}]
    history += [{"role": "user", "content": f"无关话题 {i}"} for i in range(8)]
    recognizer = IntentRecognizer("test", model="test")
    assert recognizer._cache_key("提交", history) != recognizer._cache_key("提交", history[1:])


def test_multiple_systems_with_reasons_are_an_explicit_permission_request():
    message = "工号1001，申请 GitLab 只读（代码审阅）和 Jira 只读（查看缺陷）两个权限，都提交。"
    assert explicit_write_requests(message) == {"request_access"}
    # Two targets remain two targets; do not invent a single default system.
    assert explicit_access_systems(message) == []
