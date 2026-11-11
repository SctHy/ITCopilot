"""Feishu (Lark) bot: event verification and parsing, identity mapping, and replies.

Inbound events arrive at ``POST /channels/feishu/events`` (event subscription v2.0,
``im.message.receive_v1``). Trust comes from the platform, never from message text:

- the request is authentic when its verification token matches and, with an encrypt key,
  its ``X-Lark-Signature`` matches and the body decrypts;
- the sender is the Feishu ``open_id`` / ``user_id`` in the event header, mapped to an
  IT-Copilot identity by ``config/feishu_users.yaml``. Unmapped senders get no access.

Gate (after DeepSeek dsh-superagent): the bot acts only when addressed — a direct message,
an @mention in a group, or a reply in a thread that the same person opened with the bot.
Other bots' messages, including its own, never enter.
"""
from __future__ import annotations

from core.protocols import CredentialState, credential_state

import base64
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, FrozenSet, Optional, Tuple

import httpx2
import yaml

from intake.base import ChannelError, InboundMessage
from core.identity import KNOWN_ROLES, Principal

logger = logging.getLogger(__name__)

_ROOT = pathlib.Path(__file__).resolve().parent.parent
CHANNEL = "feishu"
# Codes Feishu returns when a retry can succeed: rate limits and expired/invalid tenant tokens.
_RETRY_CODES = {99991400, 230020}
_TOKEN_CODES = {99991661, 99991663, 99991668}


class EventRejected(Exception):
    """Not an authentic Feishu event (bad token, signature or ciphertext)."""


@dataclass(frozen=True)
class FeishuSettings:
    app_id: str
    app_secret: str
    verification_token: str
    encrypt_key: str = ""
    bot_open_id: str = ""
    allowed_chats: FrozenSet[str] = field(default_factory=frozenset)   # empty: every group the bot is in
    base_url: str = "https://open.feishu.cn"
    ack_emoji: str = "OnIt"

    @classmethod
    def from_env(cls) -> Optional["FeishuSettings"]:
        if not os.getenv("FEISHU_APP_ID"):
            return None
        return cls(
            app_id=os.environ["FEISHU_APP_ID"], app_secret=os.getenv("FEISHU_APP_SECRET", ""),
            verification_token=os.getenv("FEISHU_VERIFICATION_TOKEN", ""), encrypt_key=os.getenv("FEISHU_ENCRYPT_KEY", ""),
            bot_open_id=os.getenv("FEISHU_BOT_OPEN_ID", ""),
            allowed_chats=frozenset(c.strip() for c in os.getenv("FEISHU_ALLOWED_CHATS", "").split(",") if c.strip()),
            base_url=os.getenv("FEISHU_BASE_URL", "https://open.feishu.cn"), ack_emoji=os.getenv("FEISHU_ACK_EMOJI", "OnIt"),
        )


# ── Verification ──────────────────────────────────────────────────────────────

def _decrypt(encrypt_key: str, ciphertext: str) -> Dict[str, Any]:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    try:
        raw = base64.b64decode(ciphertext)
        decryptor = Cipher(algorithms.AES(hashlib.sha256(encrypt_key.encode()).digest()), modes.CBC(raw[:16])).decryptor()
        padded = decryptor.update(raw[16:]) + decryptor.finalize()
        return json.loads(padded[:-padded[-1]].decode("utf-8"))
    except Exception as exc:
        raise EventRejected("event decryption failed") from exc


def open_event(settings: FeishuSettings, body: bytes, headers: Dict[str, str]) -> Dict[str, Any]:
    """Authenticate a webhook request and return the plaintext event."""
    if not settings.verification_token:
        raise EventRejected("FEISHU_VERIFICATION_TOKEN is not configured")
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError as exc:
        raise EventRejected("body is not JSON") from exc
    if settings.encrypt_key:
        signature = headers.get("x-lark-signature")
        if signature is not None:  # URL verification requests are not signed
            base = (headers.get("x-lark-request-timestamp", "") + headers.get("x-lark-request-nonce", "") + settings.encrypt_key).encode() + body
            if not hmac.compare_digest(hashlib.sha256(base).hexdigest(), signature):
                raise EventRejected("signature mismatch")
        if "encrypt" not in payload:
            raise EventRejected("encrypt key configured but event is plaintext")
        payload = _decrypt(settings.encrypt_key, payload["encrypt"])
    token = payload.get("token") or (payload.get("header") or {}).get("token") or ""
    if not hmac.compare_digest(str(token), settings.verification_token):
        raise EventRejected("verification token mismatch")
    return payload


# ── Parsing and gate ──────────────────────────────────────────────────────────

def _text_of(message: Dict[str, Any]) -> str:
    try:
        content = json.loads(message.get("content") or "{}")
    except json.JSONDecodeError:
        return ""
    if message.get("message_type") == "text":
        text = content.get("text", "")
    elif message.get("message_type") == "post":
        body = content.get("content") or next((v.get("content") for v in content.values() if isinstance(v, dict)), [])
        text = "\n".join("".join(n.get("text", "") for n in line if n.get("tag") in {"text", "a"}).strip() for line in body or [])
        text = (content.get("title", "") + "\n" + text).strip()
    else:
        return ""
    for mention in message.get("mentions") or []:  # "@_user_1" placeholders
        text = text.replace(mention.get("key", ""), "")
    return re.sub(r"[ \t]+", " ", text).strip()


def parse_message(settings: FeishuSettings, event: Dict[str, Any]) -> Tuple[Optional[InboundMessage], str]:
    """Return (message, "") for a candidate, or (None, reason) for an event that never enters."""
    header = event.get("header") or {}
    if header.get("event_type") != "im.message.receive_v1":
        return None, "not_a_message"
    body = event.get("event") or {}
    sender, message = body.get("sender") or {}, body.get("message") or {}
    if sender.get("sender_type") != "user":
        return None, "bot_sender"
    chat_id, chat_type, message_id = message.get("chat_id", ""), message.get("chat_type", ""), message.get("message_id", "")
    if not chat_id or not message_id:
        return None, "malformed"
    direct = chat_type == "p2p"
    if not direct and settings.allowed_chats and chat_id not in settings.allowed_chats:
        return None, "chat_not_listened"
    mentioned = any((m.get("id") or {}).get("open_id") == settings.bot_open_id for m in message.get("mentions") or []) if settings.bot_open_id else False
    root = message.get("root_id") or ""
    ids = sender.get("sender_id") or {}
    # A direct chat is one continuing conversation; in a group every @mention opens a thread
    # and replies in that thread continue it (the bot answers as a thread reply).
    conv_id = f"{CHANNEL}:{chat_id}" if direct else f"{CHANNEL}:{chat_id}:{root or message_id}"
    return InboundMessage(
        id=f"{CHANNEL}:{message_id}", channel=CHANNEL, conv_id=conv_id[:200],
        sender={"open_id": ids.get("open_id"), "user_id": ids.get("user_id"), "union_id": ids.get("union_id")},
        text=_text_of(message), address={"message_id": message_id, "chat_id": chat_id},
        addressed=direct or mentioned, thread_root=f"{CHANNEL}:{root}" if root else None,
        meta={"chat_type": chat_type, "message_type": message.get("message_type"), "event_id": header.get("event_id")},
    ), ""


# ── Identity ──────────────────────────────────────────────────────────────────

@lru_cache(maxsize=4)
def _load_directory(path: str, mtime_ns: int) -> Dict[str, Principal]:
    data = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8")) or {}
    directory = {}
    for entry in data.get("users", []):
        principal = Principal(
            subject=str(entry["subject"]), username=str(entry["username"]), department=str(entry["department"]),
            roles=frozenset(r for r in entry.get("roles", ["employee"]) if r in KNOWN_ROLES),
            employee_id=str(entry["employee_id"]) if entry.get("employee_id") else None, tenant=str(entry.get("tenant", "default")),
        )
        for key in ("open_id", "user_id"):
            if entry.get(key):
                directory[str(entry[key])] = principal
    return directory


def resolve_principal(sender: Dict[str, Any]) -> Optional[Principal]:
    """Feishu sender -> IT-Copilot identity; None when the person is not mapped."""
    path = os.getenv("FEISHU_USERS", str(_ROOT / "config/feishu_users.yaml"))
    try:
        directory = _load_directory(path, pathlib.Path(path).stat().st_mtime_ns)
    except FileNotFoundError:
        return None
    for key in ("user_id", "open_id"):
        if sender.get(key) and sender[key] in directory:
            return directory[sender[key]]
    return None


# ── Sending ───────────────────────────────────────────────────────────────────

class FeishuClient:
    """Replies through the bot's tenant access token. Feishu drops a repeated ``uuid``
    within an hour, so a resend after an ambiguous failure does not post twice."""

    name = CHANNEL

    def __init__(self, settings: FeishuSettings, *, timeout_s: float = 10.0, transport: Optional[httpx2.AsyncBaseTransport] = None):
        self.settings = settings
        self._client = httpx2.AsyncClient(base_url=settings.base_url.rstrip("/") + "/open-apis", timeout=timeout_s, transport=transport)
        self._token: Optional[str] = None
        self._token_expires = 0.0

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def token_state(self):
        return credential_state(self._token, self._token_expires, time.monotonic())

    async def _tenant_token(self) -> str:
        if self.token_state == CredentialState.VALID:
            return self._token
        data = await self._call("POST", "/auth/v3/tenant_access_token/internal",
                                json={"app_id": self.settings.app_id, "app_secret": self.settings.app_secret}, auth=False)
        self._token = data["tenant_access_token"]
        self._token_expires = time.monotonic() + int(data.get("expire", 7200)) - 300
        return self._token

    async def _call(self, method: str, path: str, *, auth: bool = True, **kwargs) -> Dict[str, Any]:
        headers = {"Authorization": f"Bearer {await self._tenant_token()}"} if auth else {}
        try:
            response = await self._client.request(method, path, headers=headers, **kwargs)
        except httpx2.TransportError as exc:
            raise ChannelError(f"飞书请求失败（{type(exc).__name__}）") from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        code = data.get("code", -1 if response.status_code >= 400 else 0)
        if code == 0 and response.status_code < 400:
            return data
        if code in _TOKEN_CODES:
            self._token = None
        retry = response.status_code == 429 or response.status_code >= 500 or code in _RETRY_CODES | _TOKEN_CODES
        raise ChannelError(f"飞书返回 {response.status_code} code={code} {data.get('msg', '')}".strip(), permanent=not retry)

    async def bot_open_id(self) -> str:
        data = await self._call("GET", "/bot/v3/info")
        return (data.get("bot") or {}).get("open_id", "")

    async def send(self, address: Dict[str, Any], text: str, idempotency_key: str) -> None:
        await self._call("POST", f"/im/v1/messages/{address['message_id']}/reply",
                         json={"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False), "uuid": idempotency_key[:50]})

    async def acknowledge(self, address: Dict[str, Any]) -> None:
        if self.settings.ack_emoji:
            await self._call("POST", f"/im/v1/messages/{address['message_id']}/reactions",
                             json={"reaction_type": {"emoji_type": self.settings.ack_emoji}})
