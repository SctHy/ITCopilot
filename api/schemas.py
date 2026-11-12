"""HTTP 请求与响应模型。"""
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator


class ChatRequest(BaseModel):
    """身份（用户、部门、角色）只来自已验证的 token；请求体里的 user_id/department 等字段会被忽略。"""
    message: str = Field(..., min_length=1, max_length=8000)
    conv_id: str = Field("demo", min_length=1, max_length=200)
    client_request_id: Optional[str] = Field(None, min_length=1, max_length=200)

    @field_validator("conv_id")
    @classmethod
    def private_conversation(cls, value):
        if value.startswith("task:"):
            raise ValueError("共享事项请使用 /tasks 接口；私人对话不能使用 task: 会话标识")
        return value


class ChatResponse(BaseModel):
    task_references: list[Dict[str, Any]] = Field(default_factory=list)
    # The caller's own withdrawable requests / releasable grants mentioned this turn (shown as cards; never acted on).
    related_approvals: list[Dict[str, Any]] = Field(default_factory=list)
    request_id: str
    response: str
    status: str
    intent: str
    intent_group: str = "other"
    entities: Dict[str, Any] = {}
    confidence: float = 0.
    intent_confidence: float = 0.
    intent_source_scores: Dict[str, float] = {}
    clarification_needed: bool = False
    missing_slots: list[str] = []
    inherited_from_previous: bool = False
    trace_id: str = ""
    tools_used: list[str] = []
    knowledge_used: bool = False
    latency_ms: float = 0.
    escalated: bool = False
    approval_required: bool = False
    approval_id: Optional[str] = None
    awaiting_confirmation: bool = False   # approval_id 是草稿：申请人确认后才进入审批人队列（ADR 0020）
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


class ApprovalConfirmation(BaseModel):
    """申请人确认 Agent 起草的申请；哈希绑定申请人在卡片上看到的冻结内容。"""
    payload_hash: str = Field(..., min_length=64, max_length=64)


class ApprovalWithdrawal(BaseModel):
    """只有申请人本人可以撤回；身份来自 token。"""
    reason: str = Field("", max_length=500)


class SessionRevocationRequest(BaseModel):
    """按 subject 或用户名（本租户最近登录过的身份）指定；两者给一个。"""
    subject: Optional[str] = Field(None, min_length=1, max_length=200)
    username: Optional[str] = Field(None, min_length=1, max_length=80)
    reason: str = Field("", max_length=500)


class DevTokenRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=80)
