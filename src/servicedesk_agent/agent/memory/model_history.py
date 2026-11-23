"""Bounded, credential-safe storage of one complete PydanticAI exchange."""
import json
from dataclasses import replace

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter, ModelRequest, ModelResponse, RetryPromptPart,
    SystemPromptPart, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart,
)

from servicedesk_agent.agent.masking.model_masking import MaskingSession, current_session
from servicedesk_agent.agent.masking.redaction import REDACTED, SECRET_FIELD, redact_text, user_secrets

MAX_BYTES = 64 * 1024


def decode_turn(encoded):
    if not isinstance(encoded, str):
        return None
    try:
        if len(encoded.encode()) > MAX_BYTES:
            return None
        messages = ModelMessagesTypeAdapter.validate_json(encoded)
    except ValueError:
        return None
    if not messages or not messages[0].parts or not isinstance(messages[0].parts[0], UserPromptPart):
        return None
    pending = {}
    for index, msg in enumerate(messages):
        if isinstance(msg, ModelResponse) != bool(index % 2) or not msg.parts:
            return None
        if isinstance(msg, ModelRequest) and msg.instructions:
            return None
        if isinstance(msg, ModelResponse) and pending:
            return None
        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                if part.tool_call_id in pending:
                    return None
                pending[part.tool_call_id] = part.tool_name
            elif isinstance(part, ToolReturnPart) or isinstance(part, RetryPromptPart) and part.tool_name:
                if part.tool_call_id not in pending or pending.pop(part.tool_call_id) != part.tool_name:
                    return None
            elif isinstance(part, SystemPromptPart) or isinstance(part, UserPromptPart) and pending:
                return None
    if pending or not isinstance(messages[-1], ModelResponse) or not any(isinstance(p, TextPart) for p in messages[-1].parts):
        return None
    return messages


def encode_turn(messages, user_text, model_prompt):
    if not messages:
        return None
    try:
        clean = [replace(m, instructions=None, parts=[p for p in m.parts if not isinstance(p, SystemPromptPart)])
                 if isinstance(m, ModelRequest) else m for m in messages]
        rows = ModelMessagesTypeAdapter.dump_python(clean, mode="json", exclude_none=True)
        if model_prompt is not None:
            rows[0]["parts"][0]["content"] = model_prompt
        # Bound traversal/regex work before inspecting nested provider or tool JSON.
        if len(json.dumps(rows, ensure_ascii=False).encode()) > MAX_BYTES:
            return None
        active = current_session()
        session = MaskingSession(active.scope)
        session.findings = active.findings
        for raw, token in active.reverse.items():
            session.remember(raw, token[2:-2].rsplit("_", 1)[0])
        secrets = user_secrets(user_text)
        # Protect short credentials echoed without labels, including echoes in a different part.
        def scrub(value, field=""):
            if SECRET_FIELD.search(field) and not isinstance(value, str):
                return REDACTED
            if isinstance(value, dict):
                return {scrub(key): scrub(v, key) for key, v in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            if isinstance(value, str):
                if value.lstrip().startswith(("{", "[")):
                    try:
                        parsed = json.loads(value)
                    except ValueError:
                        pass
                    else:
                        cleaned = scrub(parsed)
                        return value if cleaned == parsed else json.dumps(cleaned, ensure_ascii=False)
                masked = session.mask(value)
                for secret in secrets:
                    if secret in masked:
                        masked = masked.replace(secret, "[REDACTED]")
                return redact_text(masked)
            if type(value) in (int, float) and str(value) in secrets:
                return REDACTED
            return value

        session.learn(rows)
        secrets.update(raw for raw, token in session.reverse.items()
                       if token.startswith(("{{SECRET_", "{{PASSWORD_", "{{TOKEN_", "{{API_KEY_", "{{PRIVATE_KEY_")))
        cleaned = scrub(rows)
        encoded = json.dumps(cleaned, ensure_ascii=False, separators=(",", ":"))
        return encoded if decode_turn(encoded) is not None else None
    except Exception:
        # Optional cache optimization. Never log serialization errors containing model data.
        return None
