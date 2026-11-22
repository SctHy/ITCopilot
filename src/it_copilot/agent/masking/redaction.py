"""在外部文本进入存储前移除凭据和个人敏感信息。"""
import re
import json
import os
from contextvars import ContextVar

from it_copilot.agent.masking.pii_rules import business_identifier_context, chinese_id_valid, contextual_spans, secret_value_allowed

REDACTED = "[REDACTED]"
# 当前回合已识别值；只在作用域存活，不进入持久化存储。
known_secrets: ContextVar[dict | None] = ContextVar("known_secrets", default=None)
_SECRET_WORD = r"(?:密码|口令|验证码|恢复码|password|passwd|pwd|otp|token|secret|private_key)"
_SECRET_END = r"[`'\"”]?(?=[\s，,。；;！？?.]|$)"
# 显式赋值（“密码是…”“password=…”）之后的值一律视为秘密；只隔空格或 is 时，正常英文里关键词后面跟的是普通单词
# （"password at"、"otp app"），要求至少 4 个字符且含数字或符号才算秘密。全字母的口令在这种写法下会漏掉。
USER_SECRET = re.compile(
    _SECRET_WORD + r"\s*(?:是|为|:|：|=)\s*[`'\"“]?([!-~]{1,128}?)" + _SECRET_END
    + r"|" + _SECRET_WORD + r"\s+(?:is\s+)?[`'\"“]?((?=[A-Za-z]*[\d!-/:-@\[-`{-~])[!-~]{4,128}?)" + _SECRET_END,
    re.I,
)


def user_secrets(text: str) -> set:
    # Keep response scrubbing consistent with the expanded outbound/storage rules.
    return {text[a:b] for a, b, kind in sensitive_spans(text)
            if kind in {"SECRET", "PASSWORD", "TOKEN", "API_KEY", "PRIVATE_KEY"}}


_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID = re.compile(r"(?<![\dA-Za-z])\d{17}[\dXx](?![\dA-Za-z])")
_CARD = re.compile(r"(?<![A-Za-z0-9_])([2-6](?:[ -]?\d){12,18})(?![A-Za-z0-9_])")


def _card_replacement(match: re.Match) -> str:
    number = re.sub(r"[ -]", "", match.group())
    if number.startswith("2") and not (len(number) == 16 and 2221 <= int(number[:4]) <= 2720):
        return match.group()
    digits = [int(digit) for digit in reversed(number)]
    if len(set(digits)) < 2:
        return match.group()
    total = sum(digit if i % 2 == 0 else (digit * 2 - 9 if digit >= 5 else digit * 2)
                for i, digit in enumerate(digits))
    return REDACTED if total % 10 == 0 else match.group()


def redact_text(text: str) -> str:
    for start, end, _ in reversed(sensitive_spans(text)):
        text = text[:start] + REDACTED + text[end:]
    known = known_secrets.get() or {}
    if text in known:
        return REDACTED
    values = sorted((value for value, token in known.items() if len(value.encode()) >= 8
                     or (len(value) >= 2 and token.startswith(("{{PERSON_", "{{ADDRESS_")))), key=len, reverse=True)
    if values:
        text = re.sub("|".join(re.escape(value) for value in values), REDACTED, text)
    return text


_PATTERNS = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----[\s\S]+?-----END (?:[A-Z]+ )*PRIVATE KEY-----"), 0),
    ("API_KEY", re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})\b"), 0),
    # Recognizable provider prefixes from the pinned Magpie rules. Never match
    # an arbitrary high-entropy word, UUID or generic business identifier.
    ("API_KEY", re.compile(
        r"(?<![A-Za-z0-9_-])(?:AIza[0-9A-Za-z_-]{35}|xox[abposr]-[0-9A-Za-z-]{10,}"
        r"|(?:sk|rk)_live_[0-9A-Za-z]{20,}|ASIA[0-9A-Z]{16}"
        r"|hf_[A-Za-z0-9]{30,}|gsk_[A-Za-z0-9]{40,}|xai-[A-Za-z0-9]{40,}"
        r"|npm_[A-Za-z0-9]{36}|pypi-[A-Za-z0-9_-]{50,}|dop_v1_[a-f0-9]{64}"
        r"|SG\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43})(?![A-Za-z0-9_-])"), 0),
    ("TOKEN", re.compile(r"(?i)Bearer\s+([A-Za-z0-9._~+/-]+=*)"), 1),
    ("TOKEN", re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), 0),
    ("PASSWORD", re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:([^\s/@]+)@"), 1),
    ("EMAIL", re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.-])"), 0),
    ("ID", _ID, 0), ("PHONE", _MOBILE, 0), ("CARD", _CARD, 0),
]


def sensitive_spans(text: str) -> list[tuple[int, int, str]]:
    """共享检测器；只返回位置和类型，不记录原值。最长匹配优先处理重叠。"""
    found = list(contextual_spans(text))
    for match in USER_SECRET.finditer(text):
        # Explicit assignments are handled by the Unicode/quoted-value scanner.
        # Keep the legacy whitespace-only form without bypassing its negative guards.
        if match.group(2) and secret_value_allowed(text, *match.span(2)):
            found.append((*match.span(2), "SECRET"))
    for kind, pattern, group in _PATTERNS:
        for match in pattern.finditer(text):
            if kind == "CARD" and _card_replacement(match) != REDACTED:
                continue
            if kind in {"CARD", "ID", "PHONE"} and re.search(r"(?:order[_ -]?id|订单号|订单编号|工号|设备号|资产号)\s*[:：=]?\s*$", text[max(0, match.start() - 30):match.start()], re.I):
                continue
            if kind == "ID" and not chinese_id_valid(match.group()):
                # An explicitly named ID is sensitive even if a user mistypes its check digit.
                if not re.search(r"(?:身份证(?:号码|号)?|national[ _-]?id)\s*[:：=]?\s*$", text[max(0, match.start()-32):match.start()], re.I):
                    continue
            if kind == "EMAIL":
                # URL 中 user:password@host 由密码规则处理；git SSH remote 不是邮箱。
                prefix = text[:match.start()]
                if re.search(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/@]*$", prefix):
                    continue
                if match.group().startswith("git@") and text[match.end():].startswith(":"):
                    continue
            found.append((*match.span(group), kind))
    words = os.getenv("IT_COPILOT_MASK_WORDS", "[]")
    try:
        words = json.loads(words)
        if not isinstance(words, list) or any(not isinstance(w, str) or not w for w in words):
            raise ValueError
    except ValueError:
        raise ValueError("IT_COPILOT_MASK_WORDS must be a JSON array of nonempty strings") from None
    for word in words:
        found.extend((m.start(), m.end(), "TERM") for m in re.finditer(re.escape(word), text))
    chosen = []
    for item in sorted(found, key=lambda s: (s[0], -(s[1] - s[0]))):
        if item[2] in {"PHONE", "CARD", "ACCOUNT"} and business_identifier_context(text, item[0]):
            continue
        if not chosen or item[0] >= chosen[-1][1]:
            chosen.append(item)
    return chosen


def redact(value):
    if isinstance(value, dict):
        return {key: REDACTED if re.search(r'password|secret|api.?key|authorization|access.?token|验证码|密码', str(key), re.I)
                else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
