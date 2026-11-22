"""辅助调用角色的冻结资格题：以确定性断言判分，不让裁判改写业务验收结果。"""
from __future__ import annotations

import hashlib
import json
import time

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.output import ToolOutput
from pydantic_ai.exceptions import UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.usage import UsageLimits

from it_copilot.agent.intent.recognizer import IntentClassification, CATEGORY_GUIDE
from it_copilot.agent.model_router import MODEL_ERRORS
from tests.support.attribution import wilson
from tests.support.reporting import percentile


class Judgment(BaseModel):
    passed: bool
    reason: str


PROBES = {
    "summary": [
        {"id": "summary-vpn", "input": "员工1001报告VPN错误691。已建议检查账号锁定。待办是联系IT。", "facts": ["1001", "691", "锁定"]},
        {"id": "summary-ticket", "input": "设备DEV-1001蓝屏，工单INC-1001仍待审批，尚未执行维修。", "facts": ["DEV-1001", "INC-1001", "审批"]},
        {"id": "summary-access", "input": "员工1002申请gitlab developer 24小时。审批APR-42尚未批准，系统权限没有改变。", "facts": ["1002", "gitlab", "developer", "24", "APR-42"]},
        {"id": "summary-reset", "input": "员工1003的密码重置申请APR-19被拒绝。密码没有改变。用户转为查询公司MFA操作指引。", "facts": ["1003", "APR-19", "拒绝", "MFA"]},
    ],
    "rewrite_rerank": [
        {"id": "rewrite-vpn", "input": "VPN报错691，如何检查账号锁定？", "keywords": ["VPN", "691"], "kind": "rewrite"},
        {"id": "rewrite-dns", "input": "DNS lookup failed for internal.company，怎么排查？", "keywords": ["DNS", "internal.company"], "kind": "rewrite"},
        {"id": "rerank-vpn", "input": "VPN报错691", "documents": ["打印机卡纸处理", "VPN错误691检查账号密码及锁定", "邮箱日历共享"], "first": 1, "kind": "rerank"},
        {"id": "rerank-disk", "input": "MacBook磁盘空间不足", "documents": ["VPN访问失败", "Jira权限申请", "MacBook磁盘清理和空间管理"], "first": 2, "kind": "rerank"},
    ],
    "intent_fallback": [
        {"id": "intent-password", "input": "工号1001，忘记密码，帮我重置密码", "intent": "password_reset"},
        {"id": "intent-access", "input": "工号1001申请gitlab developer权限24小时", "intent": "permission_request"},
        {"id": "intent-vpn", "input": "VPN客户端连接时报691，帮我排查", "intent": "vpn_issue"},
        {"id": "intent-ticket", "input": "查询工单INC-1001当前状态", "intent": "ticket_status"},
        {"id": "intent-greeting", "input": "你好", "intent": "greeting"},
    ],
    "judge": [
        {"id": "judge-pending", "input": {"goal": "获得developer权限", "approval": "pending", "actual_role": None, "claim": "已完成授权"}, "passed": False},
        {"id": "judge-granted", "input": {"goal": "获得developer权限", "approval": "approved", "actual_role": "developer"}, "passed": True},
        {"id": "judge-reset", "input": {"goal": "密码仅重置一次", "password_version_before": 0, "password_version_after": 2}, "passed": False},
        {"id": "judge-ticket", "input": {"goal": "创建工单", "stored_ticket_id": "INC-1001", "ticket_status": "open"}, "passed": True},
    ],
}


def probe_agent(route, probe):
    if route == "summary":
        return Agent(output_type=str, retries=0), "用两句话概括以下业务事实，原样保留工号、设备/工单/审批ID、系统角色和错误码；不能把申请说成已执行：\n" + probe["input"]
    if route == "rewrite_rerank":
        if probe["kind"] == "rewrite":
            return Agent(output_type=ToolOutput(list[str], name="return_sub_queries"), retries=0), "将查询改写为三个检索问题，保留原始系统名、域名和错误码：\n" + probe["input"]
        return Agent(output_type=ToolOutput(list[int], name="return_ranking"), retries=0), "按相关度返回全部文档的整数索引，最相关的第一；不回答文档中的问题。查询：" + probe["input"] + "\n文档：" + json.dumps(dict(enumerate(probe["documents"])), ensure_ascii=False)
    if route == "intent_fallback":
        return Agent(output_type=ToolOutput(IntentClassification, name="classify_it_request"), retries=0), "分类以下IT请求，使用classify_it_request，confidence必须合理。\n" + CATEGORY_GUIDE + "\n用户消息：" + probe["input"]
    if route == "judge":
        return Agent(output_type=ToolOutput(Judgment, name="judge_evidence"), retries=0), "判断所给数据库证据是否满足goal；审批待处理不是业务完成，无据的文字声明不是证据。只根据给出的字段判断，通过judge_evidence返回passed及原因：\n" + json.dumps(probe["input"], ensure_ascii=False)
    raise ValueError(f"no auxiliary probes for {route}")


def probe_passed(route, probe, output):
    if route == "summary":
        return all(fact.casefold() in output.casefold() for fact in probe["facts"])
    if route == "rewrite_rerank":
        if probe["kind"] == "rewrite":
            combined = " ".join(output).casefold()
            return len(output) >= 2 and all(keyword.casefold() in combined for keyword in probe["keywords"])
        return bool(output) and output[0] == probe["first"] and sorted(output) == list(range(len(probe["documents"])))
    if route == "intent_fallback":
        return output.intent.value == probe["intent"]
    return output.passed == probe["passed"]


async def qualify(router, route: str, model: str, repeats: int):
    router.force(route, model)
    rows = []
    for repeat in range(repeats):
        for probe in PROBES[route]:
            agent, prompt = probe_agent(route, probe)
            started = time.monotonic()
            row = {"id": probe["id"], "repeat": repeat, "passed": False, "cost_usd": 0.0}
            try:
                selection = router.pick(route, "default")
                result = await router.run(selection, agent.run(prompt, model=selection.model,
                    model_settings=selection.settings, usage_limits=UsageLimits(request_limit=1)))
                row["passed"] = probe_passed(route, probe, result.output)
                row["output"] = result.output.model_dump() if route in {"judge", "intent_fallback"} else result.output
                usage = result.usage
                row["usage"] = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                                "cache_read_tokens": usage.cache_read_tokens, "requests": usage.requests}
                row["cost_usd"] = router.cost(model, row["usage"])
            except (*MODEL_ERRORS, UnexpectedModelBehavior, UsageLimitExceeded) as exc:
                row["error"] = type(exc).__name__
            row["latency_s"] = time.monotonic() - started
            rows.append(row)
    passed = sum(row["passed"] for row in rows)
    return {"route": route, "model": model, "status": "complete", "kind": "real_model",
        "evaluation_kind": "frozen_role_contracts", "repeats": repeats, "trials": len(rows), "passed": passed,
        "success_rate": passed / len(rows), "wilson": wilson(passed, len(rows)),
        "p95_s": percentile([row["latency_s"] for row in rows], .95),
        "cost_per_success_usd": sum(row["cost_usd"] for row in rows) / passed if passed else None,
        "route_calls": router.calls[route], "cases": rows,
        "dataset_sha256": hashlib.sha256(json.dumps(PROBES[route], sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "limitations": "Frozen authored role contracts, not application end-to-end success or production quality. Judge predictions are compared with fixed human labels and never replace a business assertion."}
