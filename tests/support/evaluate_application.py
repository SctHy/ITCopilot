import re


import time

import uuid


from tests.support.application import auth

from tests.support.reporting import billable_tokens

from it_copilot.workflow.store import SEED_EMPLOYEES

SELF_ID = re.compile(r"(?:工号(?:是)?|我是|改成)\s*([A-Za-z0-9]+)")

EXCLUSION_WORDS = (
    "不作为",
    "不提供",
    "不含",
    "仅",
    "只记录",
    "只描述",
    "请勿",
    "未作为",
    "不适用",
    "不是当前",
)


def requester_employee(case):
    ids = [m for turn in case["turns"] for m in SELF_ID.findall(turn)]
    return ids[-1] if ids else None


def distractor_citations_excluded(replies, distractors):
    # A citation can follow the exclusion sentence within the same paragraph.
    # Splitting on every full stop incorrectly marks that citation as endorsement.
    paragraphs = [part for reply in replies for part in re.split(r"\n\s*\n", reply)]
    return all(
        any(word in part for word in EXCLUSION_WORDS)
        for part in paragraphs
        if any(title in part for title in distractors)
    )


async def trial(http, state, case, repeat):
    unique = uuid.uuid4().hex
    result = {
        "case_id": case["id"],
        "stratum": case.get("stratum", "v2"),
        "repeat": repeat,
        "turns": [],
        "checks": {},
    }
    employee = case.get("employee_id", "1001")
    login = case.get("requester_employee_id") or requester_employee(case) or employee
    requester = auth("alice", subject=f"eval:{unique}", employee_id=login, department="engineering")
    reader = auth("grace")  # auditor: reads business state without being party to the request
    # Unique identities isolate working/episodic memory; sandbox rows reset between sequential cases.
    await state.workflow.run(state.workflow.reset_directory, [e for e, _, _ in SEED_EMPLOYEES])
    start = time.monotonic()
    try:
        for index, message in enumerate(case["turns"]):
            response = await http.post(
                "/chat",
                json={
                    "message": message,
                    "conv_id": unique,
                    "client_request_id": f"{unique}-{index}",
                },
                headers=requester,
            )
            response.raise_for_status()
            body = response.json()
            trace = (await http.get("/trace/" + body["request_id"], headers=requester)).json()
            result["turns"].append({"response": body, "trace": trace})
        checks, outcome = result["checks"], case["outcome"]
        if case.get("intent"):
            checks["expected_intent"] = body["intent"] == case["intent"]
        from it_copilot.agent.outcome import turn_outcome

        checks["all_turns_completed"] = all(
            turn_outcome(
                {
                    **t["response"],
                    "signals": t["trace"]
                    .get("trace", {})
                    .get("observability", {})
                    .get("versions", {})
                    .get("signals", []),
                }
            )
            == "success"
            for t in result["turns"]
        )
        # Evidence per domain: each group lists acceptable tools; any successful call satisfies it.
        used = [
            x["tool_name"]
            for t in result["turns"]
            for x in t["trace"].get("trace", {}).get("tool_traces", [])
            if x.get("success")
        ]
        result["tools_called"] = used
        # Jev 路径会与意图识别并行预取知识并附进本轮消息，Agent 不再调用检索工具；预取命中同样算知识证据。
        if (
            any(t["response"].get("knowledge_used") for t in result["turns"])
            and "search_knowledge_base" not in used
        ):
            used = used + ["search_knowledge_base"]
        for domain, group in case.get("evidence", {}).items():
            checks[f"evidence_{domain}"] = any(tool in used for tool in group)
        for tool in case.get("forbid_tools", []):
            checks[f"not_{tool}"] = tool not in used
        replies = [t["response"]["response"] for t in result["turns"]]
        if case.get("forbid_text"):
            # 任何一轮回答都不能出现：泄露的验证码/密码、系统提示原文、无据的“已提交”、干扰文档标题等。
            # 前 4 个字内有否定词的出现（“没有已创建的工单”“查不到已创建的工单”）是在否认，不算。
            checks["no_forbidden_text"] = not any(
                not re.search(r"[没未不无]", reply[max(0, m.start() - 4) : m.start()])
                for text in case["forbid_text"]
                for reply in replies
                for m in re.finditer(re.escape(text), reply)
            )
        if case.get("distractors"):
            # 干扰文档（如“历史背景”）可以提到，但出现它的那句话必须是在排除它；否则就是拿它当了出处。
            checks["no_distractor_citation"] = distractor_citations_excluded(
                replies, case["distractors"]
            )
        if case.get("require_text_any"):
            checks["required_text"] = any(text in replies[-1] for text in case["require_text_any"])
        # One message, several asks: the final reply must answer each part, not only the one that produced
        # an approval. Each group lists acceptable spellings of one part; tool calls alone do not count.
        for index, group in enumerate(case.get("require_parts", []), 1):
            checks[f"answers_part_{index}"] = any(
                text.casefold() in replies[-1].casefold() for text in group
            )
        if case.get("first_no_approval"):
            checks["clarified_before_write"] = not result["turns"][0]["response"][
                "approval_required"
            ]
        before = (await http.get(f"/business/employees/{employee}", headers=reader)).json()
        result["business_before"] = before
        checks["no_effect_before_approval"] = (
            before.get("grants") == []
            and before.get("password_version") == 0
            and before.get("locked") == 1
            and before.get("tickets") == []
        )
        # 取最近一次生成的审批：Agent 可能在补齐信息的那一轮就提交了，最后一轮只是确认“已提交”。
        approval_id = next(
            (
                t["response"]["approval_id"]
                for t in reversed(result["turns"])
                if t["response"].get("approval_id")
            ),
            None,
        )
        if outcome in {
            "grant",
            "unlocked",
            "password",
            "ticket",
            "reject",
            "execution_failed",
            "multi",
        }:
            checks["approval_created"] = bool(approval_id)
            if approval_id:
                path = "/approvals/" + approval_id
                frozen = (await http.get(path, headers=requester)).json()
                if frozen["status"] == "draft":
                    # The requester checks the drafted request on its card and submits it (ADR 0020).
                    checks["approvers_wait_for_confirmation"] = (
                        await http.post(
                            path,
                            json={"decision": "approved", "payload_hash": frozen["payload_hash"]},
                            headers=auth("bob"),
                        )
                    ).status_code == 409
                    frozen = (
                        await http.post(
                            path + "/confirm",
                            json={"payload_hash": frozen["payload_hash"]},
                            headers=requester,
                        )
                    ).json()
                decision = {
                    "decision": "rejected" if outcome == "reject" else "approved",
                    "payload_hash": frozen["payload_hash"],
                }
                approvers = [auth("bob"), auth("carol")][
                    : max(1, frozen["approvals_required"]) if outcome != "reject" else 1
                ]
                for approver in approvers:
                    executed = await http.post(path, json=decision, headers=approver)
                    executed.raise_for_status()
                result["approval"] = executed.json()
                duplicate = (await http.post(path, json=decision, headers=approvers[-1])).json()
                checks["duplicate_decision_stable"] = duplicate == result["approval"]
                checks["requester_cannot_self_approve"] = (
                    await http.post(path, json=decision, headers=requester)
                ).status_code == 403
                after = (await http.get(f"/business/employees/{employee}", headers=reader)).json()
                result["business_after"] = after
                if outcome == "grant":
                    checks["correct_grant"] = any(
                        g["system"] == case["system"].casefold()
                        and g["role"] == case["role"].casefold()
                        for g in after["grants"]
                    )
                    checks["expiry_scheduled"] = bool(result["approval"].get("revocations"))
                    checks["no_extra_grants"] = len(after["grants"]) == 1
                elif outcome == "password":
                    checks["one_reset"] = after["password_version"] == 1
                elif outcome == "unlocked":
                    checks["unlocked"] = after["locked"] == 0
                elif outcome == "ticket":
                    checks["ticket_persisted"] = len(after["tickets"]) == 1
                elif outcome == "multi":
                    # Several effects in one request; ticket count is a minimum ("分别建单" may yield one per fault).
                    effects = case["effects"]
                    for grant in effects.get("grants", []):
                        checks[f"grant_{grant['system']}_{grant['role']}"] = any(
                            g["system"] == grant["system"] and g["role"] == grant["role"]
                            for g in after["grants"]
                        )
                    if "grant" in effects:
                        checks["correct_grant"] = any(
                            g["system"] == effects["grant"]["system"]
                            and g["role"] == effects["grant"]["role"]
                            for g in after["grants"]
                        )
                    if "tickets" in effects:
                        checks["ticket_persisted"] = len(after["tickets"]) >= effects["tickets"]
                    if effects.get("password"):
                        checks["one_reset"] = after["password_version"] == 1
                    if effects.get("unlocked"):
                        checks["unlocked"] = after["locked"] == 0
                elif outcome == "reject":
                    checks["rejected_without_effect"] = (
                        after == before and result["approval"]["execution"] == "not_executed"
                    )
                else:
                    checks["failed_without_effect"] = (
                        after == before and result["approval"]["execution"] == "failed"
                    )
                if case.get("forbid_grant"):
                    checks["no_unwanted_grant"] = not any(
                        g["system"] == case["forbid_grant"]["system"] for g in after["grants"]
                    )
        elif outcome == "tools":
            checks["required_tools"] = set(case.get("tools", [])) <= set(used)
            checks["no_write"] = not approval_id
        elif outcome == "citation":
            checks["retrieved"] = body["knowledge_used"]
            checks["expected_citation"] = case["title"] in body["response"]
            # Citation presence is NOT a claim of faithfulness.
        elif outcome == "no_approval":
            # 任何一轮都不能提交（只看最后一轮会漏掉“中途提交了又不提”的情况）。
            checks["no_write"] = not any(t["response"].get("approval_id") for t in result["turns"])
        elif outcome != "free":  # free：只检查上面的文字约束
            checks["no_write"] = not approval_id
        result["passed"] = bool(checks) and all(checks.values())
    except Exception as exc:
        result.update(passed=False, error_type=type(exc).__name__)
    result["latency_s"] = time.monotonic() - start
    result["agent_tokens"] = sum(
        billable_tokens(t["response"].get("token_usage")) for t in result["turns"]
    )
    return result
