"""Labels use the existing application evaluator's expected-field vocabulary."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ROOT_CAUSES = ("intent_wrong", "slot_missing_or_default", "retrieval_miss", "retrieval_irrelevant",
               "tool_not_called", "tool_wrong_args", "false_claim", "policy_explain", "language_tone",
               "latency", "model_error", "product_gap", "not_a_bug")


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: str | None = None
    tools: list[str] = Field(default_factory=list)
    forbid_tools: list[str] = Field(default_factory=list)
    outcome: Literal["grant", "ticket", "password", "unlocked", "reject", "execution_failed", "multi", "tools", "citation", "no_approval", "free"] = "free"
    forbid_text: list[str] = Field(default_factory=list)
    evidence: dict[str, list[str]] = Field(default_factory=dict)
    system: str | None = None
    role: str | None = None
    title: str | None = None
    effects: dict = Field(default_factory=dict)


class Label(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_cause: Literal[*ROOT_CAUSES]
    severity: Literal["P0", "P1", "P2", "P3"]
    expected: Expected
    note: str = Field(default="", max_length=2000)
