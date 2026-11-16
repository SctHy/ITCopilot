"""Real-model user journeys through isolated application HTTP, with persisted evidence.

Usage: .venv/bin/python scripts/evaluate_role_journeys.py --output PATH
No production directory, approvals, or notifications are modified.
"""
import argparse
import asyncio
import json
import pathlib
import sys
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from evaluation.application import application, auth
from scripts.evaluate_long_dialogue import knowledge_docs, drain_maintenance


async def run(output, cases):
    report = {"suite": "role-journeys", "cases": [], "semantic_review": "pending"}
    output = pathlib.Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")

    for name in cases:
        row = {"id": name, "turns": [], "checks": {}}
        report["cases"].append(row)
        async with application(documents=knowledge_docs(), maintenance=True) as (http, state):
            async def request(method, path, data=None, user="alice", expected=200):
                response = await http.request(method, path, json=data, headers=auth(user))
                if response.status_code != expected:
                    raise AssertionError(f"{method} {path}: {response.status_code} {response.text[:500]}")
                return response.json()

            conv = uuid.uuid4().hex

            async def chat(message, user="alice"):
                body = await request("POST", "/chat", {"message": message, "conv_id": conv,
                                                      "client_request_id": uuid.uuid4().hex}, user)
                row["turns"].append({"user": user, "message": message, "response": body})
                await drain_maintenance(state)
                save()
                print(name, len(row["turns"]), body["status"], body["response"][:100], flush=True)
                return body

            async def approval(ident):
                return await request("GET", "/approvals/" + ident)

            async def confirm(ident):
                body = await approval(ident)
                if body["status"] == "draft":
                    body = await request("POST", "/approvals/" + ident + "/confirm",
                                         {"payload_hash": body["payload_hash"]})
                return body

            async def decide(ident, decision="approved"):
                body = await approval(ident)
                for user in ("bob", "carol")[:body["approvals_required"] if decision == "approved" else 1]:
                    body = await request("POST", "/approvals/" + ident,
                                         {"decision": decision, "payload_hash": body["payload_hash"]}, user)
                return body

            try:
                if name in {"password", "withdraw", "reject", "access_lifecycle", "access_unconfirmed", "typed_withdraw"}:
                    message = ("工号1001，MFA 还能用，但自助重置入口报错，请为我发起企业密码重置申请。"
                               if name == "password" else
                               "工号1001，帮我申请 Jira 只读权限，处理 PLAT 项目缺陷，用48小时。")
                    if name.startswith("access_"):
                        message = "工号1001，申请 GitLab developer 权限，用24小时，修复 platform 项目的 CI。另请说明 VPN-691 的含义。"
                    body = await chat(message)
                    ident = body.get("approval_id")
                    assert ident, "no approval"
                    row["checks"]["draft_created"] = (await approval(ident))["status"] == "draft"
                    await chat("先解释一下，这个流程结束前我的账号会发生变更吗？")
                    row["checks"]["no_early_effect"] = (await request("GET", "/business/employees/1001"))[
                        "password_version"] == 0
                    if name == "typed_withdraw":
                        await chat("确认")
                    await confirm(ident)
                    await chat("我在卡片上提交了，好了吗？")
                    row["checks"]["confirmed_pending"] = (await approval(ident))["status"] == "pending"
                    if name in {"withdraw", "typed_withdraw"}:
                        await request("POST", "/approvals/" + ident + "/withdraw", {"reason": "项目取消"})
                        await chat("我刚在卡片里撤回了，这个权限还会开通吗？")
                        row["checks"]["withdrawn"] = (await approval(ident))["status"] == "withdrawn"
                    elif name == "reject":
                        await decide(ident, "rejected")
                        await chat("审批人说拒绝了，现在是什么状态？不要替我重新提交。")
                        row["checks"]["rejected"] = (await approval(ident))["status"] == "rejected"
                    elif name.startswith("access_"):
                        frozen = await approval(ident)
                        row["checks"]["two_approvers"] = frozen["approvals_required"] == 2
                        await request("POST", "/approvals/" + ident,
                                      {"decision": "approved", "payload_hash": frozen["payload_hash"]}, "bob")
                        await chat("bob 已批准，我现在就可以推代码了吗？")
                        row["checks"]["one_vote_not_executed"] = (await request("GET", "/business/employees/1001"))["grants"] == []
                        await request("POST", "/approvals/" + ident,
                                      {"decision": "approved", "payload_hash": frozen["payload_hash"]}, "carol")
                        await chat("carol 也批准了，现在权限是什么状态？")
                        row["checks"]["grant_active"] = bool((await request("GET", "/business/employees/1001"))["grants"])
                        if name == "access_unconfirmed":
                            await chat("我好像点过提前归还，但不知道有没有成功，请查询系统实际状态，我现在还有权限吗？")
                        await request("POST", "/approvals/" + ident + "/release")
                        await chat("我已在卡片点提前归还，现在还有 GitLab developer 权限吗？")
                        row["checks"]["grant_returned"] = not (await request("GET", "/business/employees/1001"))["grants"]
                    else:
                        await decide(ident)
                        await chat("审批人已经批准，现在完成了吗？请查实际结果。")
                        await chat("还需要再点一次提交吗？")
                        after = await request("GET", "/business/employees/1001")
                        row["checks"]["one_reset"] = after["password_version"] == 1
                    await chat("请简短总结这次申请的最终状态，不要创建新的申请。")
                    items = (await request("GET", "/approvals"))["items"]
                    row["checks"]["no_duplicate"] = len(items) == 1
                    row["final_approval"] = await approval(ident)
                    row["business"] = await request("GET", "/business/employees/1001")
                    row["checks"]["other_employee_hidden"] = (
                        await http.get("/approvals/" + ident, headers=auth("dave"))).status_code == 403
                elif name == "shared":
                    task = await request("POST", "/tasks", {
                        "title": "多人协作：GitLab 403 排查和权限开通",
                        "message": "我是 alice 工号1001，platform 项目 GitLab 代码页面 403，VPN 已连接。先只读分析，暂时不要申请权限。",
                        "client_request_id": uuid.uuid4().hex}, expected=201)
                    base = "/tasks/" + task["id"]
                    # Register actual collaborators before inviting them.
                    for user in ("bob", "carol"):
                        await request("GET", "/auth/me", user=user)
                        await request("POST", base + "/members", {"subject": "dev:" + user, "role": "collaborator"})

                    async def settled():
                        end = time.monotonic() + 180
                        while time.monotonic() < end:
                            current = await request("GET", base)
                            if current["runs"] and all(r["status"] in {"done", "failed", "cancelled"} for r in current["runs"]):
                                assert all(r["status"] == "done" for r in current["runs"]), current["runs"]
                                return current
                            await asyncio.sleep(.25)
                        raise TimeoutError("shared task did not settle")

                    async def say(message, user="alice"):
                        posted = await request("POST", base + "/messages", {"message": message,
                                      "client_request_id": uuid.uuid4().hex}, user, expected=202)
                        current = await settled()
                        reply = next(e for e in current["events"] if e["kind"] == "assistant" and e["data"].get("run_id") == posted["id"])
                        row["turns"].append({"user": user, "message": message, "response": reply})
                        save()
                        print(name, len(row["turns"]), reply["text"][:100], flush=True)
                        return current

                    current = await settled()
                    row["turns"].append({"user": "alice", "message": task["title"],
                                         "response": [e for e in current["events"] if e["kind"] == "assistant"][-1]})
                    await say("我是 bob，补充：alice 能打开项目首页，但是仓库代码页 403。我的账号能看代码。只读排查。", "bob")
                    await say("更正一下：我刚才说 VPN 已连接不准确，当时断开了。现在重新连接仍然403。别把网络原因当成已证实。")
                    current = await say("我是 carol，成员页里 alice 是 Guest。我建议申请 Reporter，但这只是讨论，不代表已审批。", "carol")
                    row["checks"]["discussion_no_writes"] = not current["approvals"]
                    current = await say("我确认需要看代码，请为我申请 GitLab reporter，platform项目代码审阅，用24小时。")
                    assert current["approvals"], "shared request not drafted"
                    ident = current["approvals"][-1]["approval_id"]
                    await confirm(ident)
                    await say("我在申请卡片点了确认提交，现在到哪一步了？")
                    await decide(ident)
                    current = await settled()
                    await say("我刷新后已经能打开代码了，请总结哪些已确认、哪些只是猜测，现在目标完成了吗？")
                    current = await say("收到，没有新的信息，不要重复申请或追加排查步骤。")
                    row["checks"]["one_approval"] = len(current["approvals"]) == 1
                    row["checks"]["executed"] = (await approval(ident))["execution"] == "succeeded"
                    row["checks"]["outsider_hidden"] = (await http.get(base, headers=auth("dave"))).status_code in (403, 404)
                    row["workspace"] = await request("GET", base + "/workspace")
                    # The requester marks their own acceptance steps after verifying access.
                    evidence = next(e["id"] for e in current["events"]
                                    if e["kind"] == "requester" and "刷新后已经能打开代码" in e["text"])
                    await request("POST", base + "/progress", {
                        "version": row["workspace"]["progress"]["version"], "goal": "恢复 platform 代码访问",
                        "steps": [{"title": "申请人确认代码访问恢复", "status": "done", "evidence_ids": [evidence]}],
                        "blocker": "", "next_action": ""})
                    await request("POST", base + "/goal", {"version": 0, "checks": [
                        {"kind": "access_level", "system": "gitlab", "role": "reporter"}], "replan_limit": 0})
                    await request("POST", base + "/verify", {"client_request_id": uuid.uuid4().hex})
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        workspace = await request("GET", base + "/workspace")
                        if workspace["verification"]["acceptable"]:
                            break
                        await asyncio.sleep(.25)
                    row["checks"]["independent_verification"] = workspace["verification"]["acceptable"]
                    await request("POST", base + "/lifecycle", {"status": "resolved", "resolution": "申请人确认已能访问，独立权限核验通过"})
                    row["checks"]["human_resolved"] = (await request("GET", base))["status"] == "resolved"
                    row["task"] = await request("GET", base)
                    row["workspace"] = workspace
                row["passed"] = all(row["checks"].values())
            except Exception as exc:
                row["error"] = repr(exc)
                row["passed"] = False
            save()
            print("CASE", name, row["passed"], row.get("error"), row["checks"], flush=True)
    report["passed"] = all(c["passed"] for c in report["cases"])
    save()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", required=True)
    p.add_argument("--cases", default="password,withdraw,reject,shared")
    args = p.parse_args()
    load_dotenv(ROOT / ".env")
    asyncio.run(run(args.output, args.cases.split(",")))
