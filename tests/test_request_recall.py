"""Private chat surfaces the caller's own withdrawable requests and releasable grants as cards;
the model only sees them as data and has no tool to act on them."""
import asyncio
import dataclasses

from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, UserPromptPart

from agents.request_recall import RequestRecall
from evaluation.application import application, auth
from evaluation.fake_model import scripted_model
from tests.test_workflow import ALICE, BOB, ERIN, access, create, env, run  # noqa: F401 (env is a fixture)

DAVE = dataclasses.replace(ALICE, subject="dev:dave", username="dave", employee_id="1002")


def test_trigger_phrases_and_intent_decide_relevance():
    for message in ("我想撤回刚才的申请", "GitLab 权限用完了，帮我归还", "我的申请审批到哪了", "我有哪些权限"):
        assert RequestRecall.relevant(message)
    assert RequestRecall.relevant("看一下", "access_review")
    assert not RequestRecall.relevant("VPN 连不上", "vpn_issue")
    assert asyncio.run(RequestRecall(lambda p: 1 / 0).recall(ALICE, "VPN 连不上")) == []   # store not even read
    assert RequestRecall.prompt([]) == ""


def test_items_are_only_the_callers_own_and_still_actionable(env):  # noqa: F811
    store, worker, clock = env
    create(store, "pending", ALICE, [access(system="Jira")])
    granted = create(store, "granted", ALICE, [access(duration_hours=8)])
    store.decide("granted", "approved", BOB, granted["payload_hash"])
    for_alice = create(store, "by-erin", ERIN, [access(system="VPN", duration_hours=8)])   # filed by IT for alice
    store.decide("by-erin", "approved", BOB, for_alice["payload_hash"])
    create(store, "dave", DAVE, [access(employee="1002")])
    run(worker)
    items = {(i["approval_id"], i["kind"]) for i in store.requester_items(ALICE)}
    assert items == {("pending", "withdraw"), ("granted", "release"), ("by-erin", "release")}
    assert {i["approval_id"] for i in store.requester_items(ERIN)} == {"by-erin"}            # requester sees it too
    assert {i["approval_id"] for i in store.requester_items(dataclasses.replace(ALICE, tenant="other"))} == set()
    summary = next(i for i in store.requester_items(ALICE) if i["approval_id"] == "pending")["summary"]
    assert summary == "申请权限 jira readonly（工号 1001）"

    store.withdraw("pending", ALICE)
    store.release("granted", ALICE)
    run(worker)
    assert {i["approval_id"] for i in store.requester_items(ALICE)} == {"by-erin"}           # nothing left to take back there
    clock.t += 9 * 3600
    run(worker)
    assert store.requester_items(ALICE) == []                                               # expired grants are gone too


def test_chat_shows_cards_and_the_model_cannot_withdraw():
    seen = {}

    async def reply(messages, info):
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart("classify_it_request", {"intent": "permission_request", "confidence": .9})])
        seen.setdefault("prompt", [p for p in messages[-1].parts if isinstance(p, UserPromptPart)][0].content)  # alice's turn
        seen.setdefault("tools", {t.name for t in info.function_tools})
        return ModelResponse(parts=[TextPart("你有一条待审批的 Jira 权限申请，请点击下方卡片上的「撤回申请」。")])

    async def go():
        async with application(model=scripted_model(reply)) as (http, state):
            principal = ALICE
            state.workflow.create("web-1", request_id="web-1", conv_id="other-conversation", principal=principal,
                                  action_requests=[access(system="Jira")])
            response = await http.post("/chat", json={"message": "我想撤回刚才的 Jira 申请", "conv_id": "c", "client_request_id": "r1"})
            assert response.status_code == 200, response.text
            body = response.json()
            other = await http.post("/chat", json={"message": "我想撤回申请", "conv_id": "c", "client_request_id": "r2"},
                                    headers=auth("dave"))
            return body, other.json(), state.workflow.get("web-1")["status"]

    body, other, status = asyncio.run(go())
    assert [(i["approval_id"], i["kind"]) for i in body["related_approvals"]] == [("web-1", "withdraw")]
    assert body["approval_required"] is False and status == "pending"            # listing never changes anything
    assert "[本人可撤回的申请与可提前归还的权限" in seen["prompt"] and "web-1" in seen["prompt"]
    assert not {name for name in seen["tools"] if "withdraw" in name or "release" in name or "revoke" in name}
    assert other["related_approvals"] == []                                      # dave never sees alice's request
