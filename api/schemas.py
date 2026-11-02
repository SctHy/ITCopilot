"""HTTP 请求与响应模型。"""
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """身份（用户、部门、角色）只来自已验证的 token；请求体里的 user_id/department 等字段会被忽略。"""
    message: str = Field(..., min_length=1, max_length=8000)
    conv_id: str = Field("demo", min_length=1, max_length=200)
    client_request_id: Optional[str] = Field(None, min_length=1, max_length=200)


class ChatResponse(BaseModel):
    request_id: str
    response: str
    status: str
    agent_type: str
    intent: str
    intent_group: str = "other"
    entities: Dict[str, Any] = {}
    confidence: float = 0.
    intent_confidence: float = 0.
    routing_confidence: float = 0.
    intent_source_scores: Dict[str, float] = {}
    clarification_needed: bool = False
    missing_slots: list[str] = []
    inherited_from_previous: bool = False
    trace_id: str = ""
    tools_used: list[str] = []
    knowledge_used: bool = False
    latency_ms: float = 0.
    escalated: bool = False
    plan: Optional[Dict[str, Any]] = None
    findings: list[Dict[str, Any]] = []
    approval_required: bool = False
    approval_id: Optional[str] = None
    token_usage: Dict[str, int] = {}
    stage_ms: Dict[str, float] = {}     # memory_context / intent / agent / approval / commit


class KnowledgeDocument(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    content: str = Field(..., min_length=1, max_length=200_000)
    department: Optional[str] = Field(None, max_length=80)
    document_id: Optional[str] = Field(None, min_length=1, max_length=200)


class KnowledgeAddRequest(BaseModel):
    documents: list[KnowledgeDocument] = Field(..., min_length=1, max_length=100)


class ApprovalDecision(BaseModel):
    """审批人由 token 决定，不接受请求体中的 actor。"""
    decision: str = Field(..., pattern="^(approved|rejected)$")
    payload_hash: str = Field(..., min_length=64, max_length=64)


class DevTokenRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=80)
