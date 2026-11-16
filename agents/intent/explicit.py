"""Explicit, imperative write requests in a message ("帮我申请 GitLab 只读权限", "请发起密码重置申请").

The Agent may only file a write the user asked for this turn (``requested_actions``). Jev scores each
action for the whole message, so in a message with two asks ("VPN-691 怎么办；另外帮我申请权限") the
second ask's score is diluted below its threshold and a clearly stated request is refused. This
detector adds requests the user spelled out; it never removes one, and it does not loosen anything
downstream: tools still validate against the access policy, drafts still wait for the requester's
confirmation, and approvals still need approvers.

Questions ("怎么申请权限？"), refusals ("不用解锁") and deferrals ("等我确认再提交") are not requests.
"""
from __future__ import annotations

import re
from typing import FrozenSet, Iterator

from agents.intent.taxonomy import JEV_SYSTEMS

SENTENCE = re.compile(r"[^。；;！!？?\n]+[。；;！!？?]?")
# Asking how, asking whether, or saying not now. "别" is "don't", but not inside 分别/区别/特别/类别/告别/识别.
NOT_A_REQUEST = re.compile(r"怎么|如何|怎样|能否|能不能|可不可以|可以吗|是否|吗[？?。]?$|流程|需要什么|要什么|"
                           r"不用|不要|不(?:创建|执行|发起|提交|申请|重置|解锁|开通)|禁止|(?<![分区特类告识])别|先不|暂不|无需|不需要|等我|再说|再提交|之后再|以后再|回头再|[？?]$")
READ_ONLY = re.compile(r"只读(?:分析|排查|检查|核验|诊断|建议)|(?:仅|只)(?:做|作|进行)?(?:解释|咨询|分析)|"
                       r"不(?:执行|进行|做|创建)[^。；;\n]{0,50}(?:变更|写操作)")
# A deferral anywhere ("先给我看一下，等我确认再提交") holds back every request in the message.
DEFERRAL = re.compile(r"等我[^。；;]{0,8}(?:确认|看|问)|再提交|先不(?:要)?提交|先别提交|暂不提交|先给我看")
PATTERNS = {
    "request_access": re.compile(r"(?:(?<!已)(?<!已经)申请(?!的)|开通|开个|开一下|加一下)[^。；;，,]{0,120}?权限"),
    "unlock_account": re.compile(r"(?:申请|帮我|请|麻烦|提交)[^。；;，,]{0,6}?(?:解锁|解除锁定)|解锁申请"),
    "reset_password": re.compile(r"(?:发起|申请|帮我|请|麻烦)[^。；;，,]{0,6}?(?:密码重置|重置密码)|(?:密码重置|重置密码)申请|提交重置"),
    "create_ticket": re.compile(r"(?:建|创建|开|提|记)(?:一张|一个|个|张)?工单|(?:帮我|请)(?:建|开|提)单"),
    "lock_device": re.compile(r"(?:帮我|请|麻烦|申请)[^。；;，,]{0,8}?(?:远程)?锁定[^。；;，,]{0,8}?(?:设备|电脑|笔记本|DEV-)|远程锁定"),
}


def _request_sentences(message: str) -> Iterator[str]:
    if not message or DEFERRAL.search(message):
        return
    for sentence in SENTENCE.findall(message):
        if NOT_A_REQUEST.search(sentence.strip()):
            continue
        yield sentence


def explicit_write_requests(message: str) -> FrozenSet[str]:
    return frozenset(tool for sentence in _request_sentences(message)
                     for tool, pattern in PATTERNS.items() if pattern.search(sentence))


def readonly_requested(message: str) -> bool:
    """An explicit explanation-only scope overrides inferred writes, not separate positive asks."""
    return bool(READ_ONLY.search(message))


def explicit_access_systems(message: str) -> list[str]:
    """Extract only systems named inside an imperative permission request.

    A VPN diagnosis elsewhere in the same sentence must not fill a missing access
    target. Ambiguous or unsupported targets stay with the classifier/clarification.
    """
    systems = set()
    for sentence in _request_sentences(message):
        for request in PATTERNS["request_access"].finditer(sentence):
            for system in JEV_SYSTEMS:
                if system != "none" and re.search(
                    rf"(?<![A-Za-z0-9_-]){re.escape(system)}(?![A-Za-z0-9_-])", request.group(), re.I
                ):
                    systems.add(system)
    return sorted(systems) if len(systems) == 1 else []
