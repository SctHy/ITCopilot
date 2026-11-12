"""在外部文本进入存储前移除凭据和个人敏感信息。"""
import re

REDACTED = "[REDACTED]"
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
    return {match.group(1) or match.group(2) for match in USER_SECRET.finditer(text)}


_MOBILE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID = re.compile(r"(?<![\dA-Za-z])\d{17}[\dXx](?![\dA-Za-z])")
_CARD = re.compile(r"(?<!\d)\d{13,19}(?!\d)")


def _card_replacement(match: re.Match) -> str:
    digits = [int(digit) for digit in reversed(match.group())]
    total = sum(digit if i % 2 == 0 else (digit * 2 - 9 if digit >= 5 else digit * 2)
                for i, digit in enumerate(digits))
    return REDACTED if total % 10 == 0 else match.group()


def redact_text(text: str) -> str:
    text = USER_SECRET.sub(lambda match: match.group().replace(match.group(1) or match.group(2), REDACTED), text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{12,}\b', REDACTED, text)
    text = re.sub(r'(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*', 'Bearer ' + REDACTED, text)
    text = _ID.sub(REDACTED, text)
    text = _MOBILE.sub(REDACTED, text)
    return _CARD.sub(_card_replacement, text)


def redact(value):
    if isinstance(value, dict):
        return {key: REDACTED if re.search(r'password|secret|api.?key|authorization|access.?token|验证码|密码', str(key), re.I)
                else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
