"""情景记忆准入依据本轮业务证据，避免把澄清和无结论回答当成事实。"""
from dataclasses import dataclass
from typing import Optional

from agents.intent.recognizer import IntentResult
from api.schemas import ChatResponse
from core.redaction import redact_text


@dataclass(frozen=True)
class EpisodicRecord:
    answer: str
    metadata: dict


def admit_turn(response: ChatResponse, intent_result: IntentResult,
               conversation_state: dict) -> Optional[EpisodicRecord]:
    if (response.status != "success" or response.intent in {"greeting", "chitchat", "out_of_scope"}
            or conversation_state["missing_fields"] or response.clarification_needed
            or (not response.knowledge_used and not response.tools_used)):
        return None
    ticket_ids = intent_result.entities.get("ticket_id", [])
    ticket_id = ticket_ids[0] if ticket_ids else None
    importance = (0.8 if response.approval_id or ticket_id else
                  0.6 if response.knowledge_used else 0.4)
    return EpisodicRecord(redact_text(response.response)[:300], {
        "intent": response.intent, "intent_group": response.intent_group,
        "entities": intent_result.entities, "approval_id": response.approval_id,
        "ticket_id": ticket_id, "importance": importance,
        "source_request_id": response.request_id,
    })
