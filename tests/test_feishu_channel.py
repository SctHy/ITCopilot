"""Feishu channel: event authenticity, the gate, identity mapping, and stored inbound/outbound work (ADR 0009)."""
import asyncio
import base64
import hashlib
import json
import os

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient

import api.main as api
from api import state
from workflow.adapters import AdapterRegistry
from workflow.adapters.local import LocalDirectoryAdapter
from intake.base import ChannelError
from intake.feishu import EventRejected, FeishuClient, FeishuSettings, open_event, parse_message, resolve_principal
from intake.im import TEXT_ONLY, TURN_FAILED, UNBOUND, ChannelService
from tests.test_workflow import ALICE, BOB, Clock, access
from workflow.store import WorkflowStore
from workflow.worker import ExecutionWorker

SETTINGS = FeishuSettings(app_id="cli_test", app_secret="secret", verification_token="vtoken", bot_open_id="ou_bot")


def event(message_id="om_1", *, chat_type="p2p", chat_id="oc_1", text="VPN 连不上", open_id="ou_alice", user_id="alice",
          mentions=None, root_id=None, sender_type="user", message_type="text", content=None):
    return {
        "schema": "2.0",
        "header": {"event_id": "ev_" + message_id, "event_type": "im.message.receive_v1", "token": "vtoken"},
        "event": {
            "sender": {"sender_id": {"open_id": open_id, "user_id": user_id}, "sender_type": sender_type},
            "message": {"message_id": message_id, "chat_id": chat_id, "chat_type": chat_type, "message_type": message_type,
                        "content": content or json.dumps({"text": text}), "mentions": mentions or [], "root_id": root_id},
        },
    }


BOT_MENTION = [{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "IT-Copilot"}]


def message(**kwargs):
    parsed, reason = parse_message(SETTINGS, event(**kwargs))
    assert parsed is not None, reason
    return parsed


# ── verification and gate ─────────────────────────────────────────────────────

def test_plaintext_events_need_the_verification_token():
    assert open_event(SETTINGS, json.dumps({"type": "url_verification", "token": "vtoken", "challenge": "c1"}).encode(), {})["challenge"] == "c1"
    with pytest.raises(EventRejected):
        open_event(SETTINGS, json.dumps(event()).encode() .replace(b"vtoken", b"forged"), {})
    with pytest.raises(EventRejected):
        open_event(FeishuSettings(app_id="a", app_secret="s", verification_token=""), json.dumps(event()).encode(), {})


def _encrypt(key: str, payload: dict) -> bytes:
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    data = padder.update(json.dumps(payload).encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv)).encryptor()
    return json.dumps({"encrypt": base64.b64encode(iv + encryptor.update(data) + encryptor.finalize()).decode()}).encode()


def test_encrypted_events_are_signed_and_decrypted():
    settings = FeishuSettings(app_id="a", app_secret="s", verification_token="vtoken", encrypt_key="ekey")
    body = _encrypt("ekey", event())
    headers = {"x-lark-request-timestamp": "1700000000", "x-lark-request-nonce": "n1"}
    headers["x-lark-signature"] = hashlib.sha256(("1700000000" + "n1" + "ekey").encode() + body).hexdigest()
    assert open_event(settings, body, headers)["header"]["event_type"] == "im.message.receive_v1"
    with pytest.raises(EventRejected, match="signature"):
        open_event(settings, body, {**headers, "x-lark-signature": "0" * 64})
    with pytest.raises(EventRejected, match="plaintext"):
        open_event(settings, json.dumps(event()).encode(), {})
    with pytest.raises(EventRejected, match="decryption"):
        open_event(settings, _encrypt("other-key", event()), {})


def test_gate_admits_direct_messages_mentions_and_nothing_from_bots():
    direct = message()
    assert direct.addressed and direct.conv_id == "feishu:oc_1" and direct.text == "VPN 连不上" and direct.id == "feishu:om_1"
    chatter = message(chat_type="group", text="今天食堂吃什么")
    assert not chatter.addressed
    mention = message(message_id="om_2", chat_type="group", text="@_user_1 VPN 连不上", mentions=BOT_MENTION)
    assert mention.addressed and mention.text == "VPN 连不上" and mention.conv_id == "feishu:oc_1:om_2"
    reply = message(message_id="om_3", chat_type="group", text="工号 1001", root_id="om_2")
    assert reply.conv_id == "feishu:oc_1:om_2" and reply.thread_root == "feishu:om_2"
    assert parse_message(SETTINGS, event(sender_type="app"))[1] == "bot_sender"
    narrowed = FeishuSettings(app_id="a", app_secret="s", verification_token="v", bot_open_id="ou_bot", allowed_chats=frozenset({"oc_it"}))
    assert parse_message(narrowed, event(chat_type="group", mentions=BOT_MENTION))[1] == "chat_not_listened"
    assert parse_message(narrowed, event())[0].addressed  # direct messages are not limited by chat
    post = {"title": "报障", "content": [[{"tag": "at", "user_id": "@_user_1"}, {"tag": "text", "text": " 打印机卡纸"}]]}
    assert message(message_type="post", content=json.dumps(post)).text == "报障\n打印机卡纸"
    assert message(message_type="image", content="{}").text == ""


def test_identity_comes_from_the_mapping_file_only(tmp_path, monkeypatch):
    path = tmp_path / "users.yaml"
    path.write_text("users:\n  - {user_id: u1, open_id: ou_1, subject: 'dev:alice', username: alice, employee_id: '1001', department: engineering, roles: [employee, it_admin, root]}\n")
    monkeypatch.setenv("FEISHU_USERS", str(path))
    principal = resolve_principal({"open_id": "ou_1"})
    assert principal.subject == "dev:alice" and principal.roles == frozenset({"employee", "it_admin"})
    assert resolve_principal({"user_id": "u1"}) == principal
    assert resolve_principal({"open_id": "ou_unknown"}) is None


# ── Feishu HTTP client ────────────────────────────────────────────────────────

def test_client_classifies_failures_and_refreshes_the_token():
    calls, replies = [], iter([
        httpx.Response(200, json={"code": 0}),
        httpx.Response(400, json={"code": 230011, "msg": "message recalled"}),
        httpx.Response(400, json={"code": 99991663, "msg": "invalid token"}),
        httpx.Response(503, text="busy"),
    ])

    def handler(request):
        calls.append((request.url.path, request.headers.get("authorization"), request.content))
        if request.url.path.endswith("tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": f"t{len(calls)}", "expire": 7200})
        return next(replies)

    async def scenario():
        client = FeishuClient(SETTINGS, transport=httpx.MockTransport(handler))
        await client.send({"message_id": "om_1"}, "你好", "k" * 64)
        with pytest.raises(ChannelError) as recalled:
            await client.send({"message_id": "om_1"}, "你好", "k2")
        with pytest.raises(ChannelError) as expired:
            await client.send({"message_id": "om_1"}, "你好", "k3")
        with pytest.raises(ChannelError) as busy:
            await client.send({"message_id": "om_1"}, "你好", "k4")
        await client.close()
        return recalled.value, expired.value, busy.value

    recalled, expired, busy = asyncio.run(scenario())
    assert recalled.permanent and not expired.permanent and not busy.permanent
    body = json.loads(calls[1][2])
    assert calls[1][0] == "/open-apis/im/v1/messages/om_1/reply" and body["uuid"] == "k" * 50 and json.loads(body["content"]) == {"text": "你好"}
    token_fetches = [c for c in calls if c[0].endswith("tenant_access_token/internal")]
    assert len(token_fetches) == 2  # fetched once, again after the invalid-token answer


# ── stored work ───────────────────────────────────────────────────────────────

class FakeSender:
    name = "feishu"

    def __init__(self):
        self.sent, self.acks, self.failures = [], [], []

    async def send(self, address, text, key):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((address["message_id"], text))

    async def acknowledge(self, address):
        self.acks.append(address["message_id"])


@pytest.fixture
def channel(db_url):
    clock = Clock()
    store = WorkflowStore(db_url, clock=clock)
    worker = ExecutionWorker(store, AdapterRegistry(LocalDirectoryAdapter(store.engine)), worker_id="w1", base_delay_s=0)
    sender, turns = FakeSender(), []

    async def handle(principal, text, conv_id, client_request_id):
        turns.append((principal.subject, text, conv_id, client_request_id))
        if "申请" in text:  # what /chat does for a write: freeze an approval under the caller's identity
            rid = "rid-" + client_request_id
            store.create(rid + ":approval", request_id=rid, conv_id=conv_id, principal=principal, action_requests=[access()])
            return {"request_id": rid, "response": "已生成 GitLab 只读权限申请。", "approval_required": True, "approval_id": rid + ":approval"}
        if "崩溃" in text:
            raise RuntimeError("model unavailable")
        return {"request_id": "rid-" + client_request_id, "response": "请检查 VPN 客户端版本。", "approval_required": False}

    directory = {"ou_alice": ALICE, "ou_bob": BOB}
    service = ChannelService(store.engine, {"feishu": sender}, handle, {"feishu": lambda s: directory.get(s.get("open_id"))}, clock=clock)
    store.notify = service.notify_approval
    return service, sender, turns, store, worker, clock


def drain(service):
    asyncio.run(service.run_until_idle())


def test_message_is_answered_once_even_when_the_platform_retries(channel):
    service, sender, turns, *_ = channel
    assert service.accept(message()) == "queued"
    assert service.accept(message()) == "duplicate"
    drain(service)
    assert turns == [("dev:alice", "VPN 连不上", "feishu:oc_1", "feishu:om_1")]
    assert sender.acks == ["om_1"] and sender.sent == [("om_1", "请检查 VPN 客户端版本。")]
    drain(service)
    assert len(sender.sent) == 1


def test_approval_outcome_is_reported_back_to_the_asking_conversation(channel):
    service, sender, _, store, worker, _ = channel
    service.accept(message(text="申请 GitLab 只读权限"))
    drain(service)
    assert "已提交审批（审批单 rid-feis）" in sender.sent[0][1]
    approval = store.list(BOB, "pending")[0]
    store.decide(approval["approval_id"], "approved", BOB, approval["payload_hash"])
    asyncio.run(worker.run_until_idle())
    drain(service)
    assert sender.sent[-1] == ("om_1", "审批单 rid-feis 已批准并执行完成：gitlab readonly 权限已开通。")
    asyncio.run(worker.run_until_idle())
    drain(service)
    assert len(sender.sent) == 2  # one answer, one notice


def test_rejection_and_web_approvals_notify_only_their_own_conversation(channel):
    service, sender, _, store, _, _ = channel
    service.accept(message(text="申请 GitLab 只读权限"))
    drain(service)
    store.create("web:approval", request_id="web", conv_id="demo", principal=ALICE, action_requests=[access(system="jira")])
    for approval in store.list(BOB, "pending"):
        store.decide(approval["approval_id"], "rejected", BOB, approval["payload_hash"])
    drain(service)
    assert sender.sent[-1][1] == "审批单 rid-feis 未通过审批。" and len(sender.sent) == 2


def test_unbound_senders_and_non_text_get_one_short_answer(channel):
    service, sender, turns, *_ = channel
    service.accept(message(open_id="ou_stranger"))
    service.accept(message(message_id="om_2", message_type="image", content="{}"))
    drain(service)
    assert turns == [] and [t for _, t in sender.sent] == [UNBOUND, TEXT_ONLY]


def test_group_chatter_is_never_stored_but_the_askers_thread_replies_continue(channel):
    service, sender, turns, *_ = channel
    assert service.accept(message(chat_type="group", text="今天食堂吃什么")) == "ignored"
    service.accept(message(message_id="om_2", chat_type="group", text="@_user_1 VPN 连不上", mentions=BOT_MENTION))
    assert service.accept(message(message_id="om_3", chat_type="group", text="客户端是 5.2", root_id="om_2")) == "queued"
    assert service.accept(message(message_id="om_4", chat_type="group", text="我也是", root_id="om_2", open_id="ou_bob")) == "ignored"
    drain(service)
    assert [t[1:3] for t in turns] == [("VPN 连不上", "feishu:oc_1:om_2"), ("客户端是 5.2", "feishu:oc_1:om_2")]


def test_failed_turns_retry_then_tell_the_person(channel):
    service, sender, turns, _, _, clock = channel
    service.accept(message(text="崩溃"))
    for _ in range(3):
        drain(service)
        clock.t += 600
    drain(service)
    assert len(turns) == 3 and sender.sent == [("om_1", TURN_FAILED)]
    assert service.problems()["failed_messages"][0]["id"] == "feishu:om_1"


def test_replies_keep_order_back_off_and_drop_when_the_conversation_is_gone(channel):
    service, sender, _, _, _, clock = channel
    sender.failures = [ChannelError("timeout")]
    service.accept(message(message_id="om_1"))
    service.accept(message(message_id="om_2", text="还是不行"))
    drain(service)
    assert sender.sent == []  # om_1's reply failed; om_2's waits behind it
    clock.t += 5
    drain(service)
    assert [m for m, _ in sender.sent] == ["om_1", "om_2"]

    sender.failures = [ChannelError("message recalled", permanent=True)]
    service.accept(message(message_id="om_9", chat_id="oc_2"))
    drain(service)
    assert len(sender.sent) == 2 and service.problems()["undelivered"] == []


def test_a_message_failing_repeatedly_stops_blocking_the_conversation(channel):
    service, sender, _, _, _, clock = channel
    sender.failures = [ChannelError("busy")] * 3
    service.accept(message(message_id="om_1"))
    for _ in range(3):
        drain(service)
        clock.t += 300
    service.accept(message(message_id="om_2", text="还是不行"))
    sender.failures = [ChannelError("busy")] * 10
    drain(service)
    sender.failures = []
    clock.t += 300
    drain(service)
    assert {m for m, _ in sender.sent} == {"om_1", "om_2"}
    assert service.problems()["undelivered"] == []


# ── webhook endpoint ──────────────────────────────────────────────────────────

def test_webhook_verifies_then_only_stores(channel, monkeypatch):
    service, *_ = channel
    client = FeishuClient(SETTINGS)
    monkeypatch.setattr(state, "channels", service)
    monkeypatch.setattr(state, "feishu", client)
    http = TestClient(api.app)
    assert http.post("/channels/feishu/events", json={"type": "url_verification", "token": "vtoken", "challenge": "abc"}).json() == {"challenge": "abc"}
    assert http.post("/channels/feishu/events", json={**event(), "header": {**event()["header"], "token": "x"}}).status_code == 401
    assert http.post("/channels/feishu/events", json=event()).json() == {"ok": True, "outcome": "queued"}
    assert http.post("/channels/feishu/events", json=event()).json() == {"ok": True, "outcome": "duplicate"}
    assert http.post("/channels/feishu/events", json=event(sender_type="app")).json()["ignored"] == "bot_sender"
    asyncio.run(client.close())


def test_webhook_is_absent_when_feishu_is_not_configured(monkeypatch):
    monkeypatch.setattr(state, "channels", None)
    assert TestClient(api.app).post("/channels/feishu/events", json={}).status_code == 404
