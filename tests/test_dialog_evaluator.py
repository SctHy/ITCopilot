"""LLMJudge：正常评分、格式不合规时重试后判失败、分数越界视为不合规。模型用 FunctionModel 替身。"""
import asyncio
import json

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from evaluation.evaluator import LLMJudge


def judge_with(*replies):
    calls = []

    async def respond(messages, info):
        calls.append(messages)
        reply = replies[min(len(calls), len(replies)) - 1]
        return ModelResponse(parts=[TextPart(reply if isinstance(reply, str) else json.dumps(reply))])

    judge = LLMJudge(FunctionModel(respond), "judge-test")
    return judge, calls


SCORES = {"relevance": .9, "accuracy": .8, "completeness": .7, "helpfulness": .9}


def test_valid_scores_are_parsed():
    judge, calls = judge_with(SCORES)
    scores = asyncio.run(judge.judge("VPN 连不上", "先检查账号锁定", context="VPN SOP"))
    assert not scores.judge_failed and scores.attempts == 1 and len(calls) == 1
    assert (scores.relevance, scores.accuracy, scores.completeness, scores.helpfulness) == (.9, .8, .7, .9)


def test_non_json_output_is_retried_once_then_succeeds():
    judge, calls = judge_with("我觉得回答还不错", SCORES)
    scores = asyncio.run(judge.judge("VPN 连不上", "先检查账号锁定"))
    assert not scores.judge_failed and scores.attempts == 2
    assert "格式修正要求" in calls[1][-1].parts[-1].content


def test_persistently_invalid_output_marks_judge_failed():
    judge, calls = judge_with({"relevance": 1.5, "accuracy": .8, "completeness": .7, "helpfulness": .9})
    scores = asyncio.run(judge.judge("VPN 连不上", "先检查账号锁定"))
    assert scores.judge_failed and len(calls) == 2 and scores.relevance is None
